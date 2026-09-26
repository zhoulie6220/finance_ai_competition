"""把数据包里的**不可再生数据**导进当前库。

用法：
    python scripts/merge_data_pack.py --source <数据包>/backend/var/finance.db
    python scripts/merge_data_pack.py --source <...> --dry-run
    python scripts/merge_data_pack.py --source <...> --skip financial_fact

为什么需要这个脚本
------------------
`init_db.py --force` 是重建，会把 financial_fact / document_page / mdna_section
等表整个清空。而数据包说明里写的恢复路径依赖 scripts/parse_reports.py 与
scripts/parse_mdna.py —— **这两个脚本不存在**。

所以数据包里的这批数据是唯一来源。改完 schema 或种子之后的固定动作是：

    python scripts/init_db.py --force
    python scripts/merge_data_pack.py --source <数据包>/backend/var/finance.db
    python scripts/verify_db.py

什么不复制
----------
**metric_definition 与 rule_config 绝不复制**——它们归 app/data/seed/ 所有。
这份脚本因此可以在改完种子之后安全重跑：种子赢，数据包里那份陈旧的
不会把新种子盖掉。（实测数据包里的种子确实比仓库旧：89/65 vs 91/66。）

page_fts 也不复制——它是 page_fts 虚拟表的影子表，由 trg_page_fts_insert
等触发器在插 document_page 时自动维护。所以 document_page **必须走正常插入
路径**，不能为了快关掉触发器，否则全文检索会静默变空。

遇到 schema 漂移会**大声失败**
------------------------------
如果目标库里缺表、或者列对不上，本脚本直接报错退出，不跳过。跳过的后果是
「导完了、看着像成功、实际少了 14 行」——这正是本项目反复防的那种静默损坏。
用 --skip 显式跳过才允许。

退出码
------
0   成功（或 --dry-run 检查通过）
1   拒绝执行：目标非空 / 缺表 / 列不一致 / 外键违规
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

# 让脚本能直接以 `python scripts/merge_data_pack.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import connect  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"

# 复制顺序 = 外键依赖顺序（被引用的表在前）。
# task 必须在 task_step / tool_call 之前；file 必须在 document_page /
# financial_fact / mdna_section 之前；financial_fact 必须在 fact_observation 之前。
COPY_ORDER: tuple[str, ...] = (
    "project",
    "file",
    "document_page",       # 触发 trg_page_fts_insert，自动维护 page_fts
    "financial_fact",
    "fact_observation",
    "mdna_section",
    "task",
    "task_step",
    "tool_call",
    "app_log",
    "metric_key_migration",
)

# 种子拥有的表：绝不从数据包复制，否则会把新的种子覆盖成旧的。
SEED_OWNED: tuple[str, ...] = ("metric_definition", "rule_config")


def main() -> int:
    parser = argparse.ArgumentParser(description="把数据包的不可再生数据导进当前库")
    parser.add_argument("--source", required=True, help="数据包里的 finance.db")
    parser.add_argument("--target", default=None, help=f"目标库，默认 {DB_PATH}")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不写库")
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="表名",
        help="显式跳过某张表（可重复）。仅用于 schema 尚未补齐时的临时验证",
    )
    args = parser.parse_args()

    source = Path(args.source)
    target = Path(args.target) if args.target else DB_PATH

    if not source.exists():
        print(f"✗ 数据包不存在：{source}", file=sys.stderr)
        return 1
    if not target.exists():
        print(f"✗ 目标库不存在：{target}", file=sys.stderr)
        print("  先跑：python scripts/init_db.py --force", file=sys.stderr)
        return 1

    plan = [t for t in COPY_ORDER if t not in args.skip]
    if args.skip:
        unknown = [t for t in args.skip if t not in COPY_ORDER]
        if unknown:
            print(f"✗ --skip 里有不在复制清单中的表：{unknown}", file=sys.stderr)
            return 1
        print(f"⚠ 显式跳过：{', '.join(args.skip)}")
        print()

    con = connect(target)
    try:
        con.execute("ATTACH DATABASE ? AS src", (str(source),))

        # ---- 前置校验：缺表 / 列不一致 / 目标非空，任一不过就拒绝执行 ----
        problem = _precheck(con, plan)
        if problem:
            print(f"✗ {problem}", file=sys.stderr)
            return 1

        counts = {t: _count(con, "src", t) for t in plan}

        if args.dry_run:
            print(f"数据包：{source}")
            print(f"目标库：{target}")
            print()
            print("将要复制（--dry-run，未写库）：")
            for t in plan:
                print(f"  {t:<24} {counts[t]:>6}")
            print()
            print(f"  合计 {sum(counts.values())} 行")
            return 0

        # ---- 复制：单事务，任一步失败整体回滚 ----
        current = "(尚未开始)"
        try:
            con.execute("BEGIN")
            for t in plan:
                current = t
                moved = _copy_table(con, t)
                if moved != counts[t]:
                    raise RuntimeError(
                        f"读了 {counts[t]} 行但只写入 {moved} 行——触发器或约束吃掉了数据"
                    )
            orphans = con.execute("PRAGMA foreign_key_check").fetchall()
            if orphans:
                raise RuntimeError(f"外键违规：{orphans[:5]}")
            con.commit()
        except Exception as exc:
            con.rollback()
            # 必须报出**是哪张表**：「FOREIGN KEY constraint failed」不带表名时，
            # 排查要从 11 张表里猜。最常见的原因是它引用的表被 --skip 掉了。
            print(
                f"✗ 导入表 {current} 时失败，已整体回滚，目标库未改动。",
                file=sys.stderr,
            )
            print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
            if "FOREIGN KEY" in str(exc):
                deps = _depends_on(current)
                print(
                    f"  提示：{current} 引用 {deps}。被引用的表若没导入（或被 --skip 掉），"
                    f"它就会失败。",
                    file=sys.stderr,
                )
            return 1

        print(f"数据包：{source}")
        print(f"目标库：{target}")
        print()
        print("已复制：")
        total = 0
        for t in plan:
            print(f"  {t:<24} {counts[t]:>6}")
            total += counts[t]
        print()
        print(f"  合计 {total} 行")
        print()
        if args.skip:
            print("⚠ 跳过的表仍未导入，数据不完整——补齐 schema 后重跑本脚本。")
        else:
            print("下一步：python scripts/verify_db.py")
        return 0
    finally:
        con.close()


def _precheck(con: sqlite3.Connection, plan: list[str]) -> str | None:
    """返回第一处问题的中文说明；全过则返回 None。"""
    for table in plan:
        src_cols = _columns(con, "src", table)
        if src_cols is None:
            return f"数据包里没有表 {table}，--source 指错了？"

        dst_cols = _columns(con, "main", table)
        if dst_cols is None:
            return (
                f"schema.sql 没有定义表 {table}，但数据包里有 "
                f"{_count(con, 'src', table)} 行。请先补 schema.sql 再重跑"
                f"（跳过会静默丢掉这些行）。"
            )

        if src_cols != dst_cols:
            only_src = [c for c in src_cols if c not in dst_cols]
            only_dst = [c for c in dst_cols if c not in src_cols]
            detail = []
            if only_src:
                detail.append(f"数据包多出列 {only_src}")
            if only_dst:
                detail.append(f"schema 多出列 {only_dst}")
            if not detail:  # 列名一样但顺序不同
                detail.append("列顺序不同")
            return f"表 {table} 的列与 schema.sql 不一致：{'；'.join(detail)}"

        existing = con.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]
        if existing:
            return (
                f"目标库的 {table} 已有 {existing} 行，拒绝导入——"
                f"重复导入会让事实双倍计数且不报错。"
                f"先 python scripts/init_db.py --force，或加 --target 指向空库。"
            )
    return None


def _columns(con: sqlite3.Connection, schema: str, table: str) -> list[str] | None:
    """取列名。表不存在返回 None。"""
    try:
        return [r[1] for r in con.execute(f'PRAGMA {schema}.table_info("{table}")')]
    except sqlite3.OperationalError:
        return None


def _count(con: sqlite3.Connection, schema: str, table: str) -> int:
    return con.execute(f'SELECT COUNT(*) FROM {schema}."{table}"').fetchone()[0]


def _depends_on(table: str) -> str:
    """从 schema.sql 里读出该表引用了哪些表，用于外键失败时的提示。

    刻意从 DDL 解析而不是写死一张依赖表——写死的那张会和 schema 分叉，
    而且是静默分叉。
    """
    schema_sql = (BACKEND_DIR / "app" / "db" / "schema.sql").read_text(encoding="utf-8")
    # 取该表自己的 CREATE TABLE 语句块，在其中找 REFERENCES <表名>
    blocks = re.findall(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+\w+\s*\(.*?\n\);", schema_sql, re.S)
    for block in blocks:
        if re.search(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+" + re.escape(table) + r"\s*\(", block):
            refs = sorted(set(re.findall(r"REFERENCES\s+(\w+)", block)))
            return "、".join(refs) if refs else "（无外键）"
    return "（未在 schema.sql 里找到该表）"


def _copy_table(con: sqlite3.Connection, table: str) -> int:
    """整表复制，返回写入行数。走正常 INSERT，让触发器和 CHECK 照常生效。"""
    cols = _columns(con, "main", table) or []
    collist = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join("?" * len(cols))
    rows = con.execute(f'SELECT {collist} FROM src."{table}"').fetchall()
    con.executemany(
        f'INSERT INTO main."{table}" ({collist}) VALUES ({placeholders})', rows
    )
    return len(rows)


if __name__ == "__main__":
    raise SystemExit(main())
