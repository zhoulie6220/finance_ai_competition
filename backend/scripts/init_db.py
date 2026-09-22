"""初始化 SQLite 数据库。

用法：
    python scripts/init_db.py            # 不存在则创建
    python scripts/init_db.py --force    # 存在则删除重建（会丢数据）

数据库是单文件，位于 backend/var/finance.db（见 .env 的 DATA_ROOT）。
不需要安装任何 SQL 软件——用的是 Python 标准库 sqlite3。

连接一律经由 app.db.session.connect()，它在**每条连接**上开启外键约束。
直接 sqlite3.connect() 会得到外键关闭的连接，全部外键约束静默失效。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能直接以 `python scripts/init_db.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _console  # noqa: E402  (与本文件同目录)

_console.setup()

from app.db.dictionary import validate as validate_dictionary  # noqa: E402
from app.db.session import DB_PATH_ENV_HINT, connect, init_schema, load_seeds  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化投研工作台数据库")
    parser.add_argument(
        "--force",
        action="store_true",
        help="若数据库已存在则删除重建（会丢失全部数据）",
    )
    args = parser.parse_args()

    if DB_PATH.exists() and not args.force:
        print(f"数据库已存在：{DB_PATH}")
        print("如需重建，请加 --force（会丢失全部数据）")
        return 0

    # 先建到临时文件，校验全过了才替换掉旧库。
    #
    # 不先删旧库是有原因的：种子文件写坏时校验会失败，如果此时旧库已经删了，
    # 就变成「坏字典 + 没有数据库」——原本还能跑的库被一次手滑毁了。
    # 顺带也让 --force 幂等：任何一步失败，旧库原封不动。
    tmp_path = DB_PATH.with_name(DB_PATH.name + ".building")
    for suffix in ("", "-wal", "-shm"):
        stale = tmp_path.with_name(tmp_path.name + suffix)
        if stale.exists():
            stale.unlink()

    # 建库与校验都在 _build() 里完成并**关掉连接**——Windows 上文件句柄没释放
    # 就删不掉半成品，所以不能在这里用 try/finally 里 return 的写法。
    ok, seeds, stats = _build(tmp_path)
    if not ok:
        return _abort(tmp_path)

    # 校验全过，这时才动旧库。WAL 模式会留下旁支文件，一并清掉，
    # 否则新库会继承旧库的 WAL。
    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"已删除旧数据库：{DB_PATH}")
    for suffix in ("-wal", "-shm"):
        sidecar = DB_PATH.with_name(DB_PATH.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    tmp_path.replace(DB_PATH)

    print(f"数据库已创建：{DB_PATH}")
    for k, v in stats.items():
        print(f"  {k} {v}")
    for seed in seeds:
        print(f"  ← {seed}")
    print(f"  外键约束 已开启，完整性检查通过")
    print()
    print(DB_PATH_ENV_HINT)
    return 0


def _build(tmp_path) -> tuple[bool, list[str], dict[str, int]]:
    """在 tmp_path 建库并跑全部自检，返回 (是否通过, 种子文件名, 统计)。

    无论成功失败都保证连接已关闭——调用方要靠这一点才能删掉半成品文件。
    """
    con = connect(tmp_path)
    seeds: list[str] = []
    stats: dict[str, int] = {}
    try:
        init_schema(con)
        seeds = load_seeds(con)
        con.commit()

        # 自检：外键约束必须处于开启状态，否则全部 FK 形同虚设
        if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            print("✗ 外键约束未开启，数据库不可用", file=sys.stderr)
            return False, seeds, stats

        orphans = con.execute("PRAGMA foreign_key_check").fetchall()
        if orphans:
            print(f"✗ 种子数据存在外键违规：{orphans[:5]}", file=sys.stderr)
            return False, seeds, stats

        # 自检：字段字典的规则。外键与 CHECK 拦不住的那几条（别名被两个字段
        # 共用、排除词挡住自己的别名、文本型字段声明了数值单位）在这里拦。
        # 它们都属于「不会报错、只会让解析静默出错」那一类。
        dict_problems = validate_dictionary(con)
        if dict_problems:
            print("✗ 字段字典未通过校验：", file=sys.stderr)
            for p in dict_problems:
                print(f"  · {p}", file=sys.stderr)
            return False, seeds, stats

        stats = {
            "表": con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts_%'"
            ).fetchone()[0],
            "视图": con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='view'"
            ).fetchone()[0],
            "字段字典": con.execute("SELECT COUNT(*) FROM metric_definition").fetchone()[0],
            "规则参数": con.execute("SELECT COUNT(*) FROM rule_config").fetchone()[0],
        }
        return True, seeds, stats
    finally:
        con.close()


def _abort(tmp_path) -> int:
    """校验未通过：丢弃半成品，旧库保持原样。"""
    for suffix in ("", "-wal", "-shm"):
        stale = tmp_path.with_name(tmp_path.name + suffix)
        if stale.exists():
            stale.unlink()
    if DB_PATH.exists():
        print(f"  旧数据库未改动：{DB_PATH}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
