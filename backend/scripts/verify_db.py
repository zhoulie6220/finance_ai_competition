"""数据库自检：确认那批**不可再生**的数据还在。

用法：
    python scripts/verify_db.py

为什么需要这个脚本
------------------
`init_db.py --force` 是**重建**——它把 financial_fact、document_page、
mdna_section 等表整个清空。而数据包说明里写的恢复路径是：

    python scripts/init_db.py --force
    python scripts/parse_reports.py --source var/samples
    python scripts/parse_mdna.py

后两个脚本**不存在**，仓库里从来没有过。所以这 743 条财务事实、3547 页正文、
399 个 MD&A 段落目前只存在于 backend/var/finance.db 一个文件里，一旦被清空，
没有任何代码能把它们变回来。

本脚本检查的不是「库能不能打开」，而是「数据还在不在」——空库是能打开的。

因此：每次跑完 init_db.py --force（或任何动过 schema / 种子的操作）之后都要跑一遍。
库不对时，用 scripts/merge_data_pack.py 从数据包恢复。

退出码
------
0   数据完整
1   任一项不符——多半是库被重建过而没重新导入数据包
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 让脚本能直接以 `python scripts/verify_db.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import connect  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"
SAMPLES_DIR = BACKEND_DIR / "var" / "samples"

# ---------------------------------------------------------------- 检查清单

SampleCount = tuple[str, str, int]

# 数据完整性：来自数据包，**没有任何代码能重新生成**，少一条都不行。
# 期望值取自 20260925 数据包，是精确值不是下限——精确值才能抓住「重复导入
# 导致双倍计数」这种同样不会报错的损坏。
DATA_CHECKS: tuple[SampleCount, ...] = (
    ("project", "SELECT COUNT(*) FROM project", 3),
    ("file", "SELECT COUNT(*) FROM file", 16),
    ("document_page", "SELECT COUNT(*) FROM document_page", 3547),
    ("page_fts", "SELECT COUNT(*) FROM page_fts", 3547),
    ("financial_fact", "SELECT COUNT(*) FROM financial_fact", 743),
    ("fact_observation", "SELECT COUNT(*) FROM fact_observation", 1309),
    ("mdna_section", "SELECT COUNT(*) FROM mdna_section", 399),
    ("task", "SELECT COUNT(*) FROM task", 14),
)

# 派生视图：schema 定义出来的，但对不上就说明数据层已经不对了。
VIEW_CHECKS: tuple[SampleCount, ...] = (
    ("v_fact_verified", "SELECT COUNT(*) FROM v_fact_verified", 743),
    (
        "v_fact_grid 有值格",
        "SELECT COUNT(*) FROM v_fact_grid WHERE fact_id IS NOT NULL",
        374,
    ),
)

# 种子：来自 app/data/seed/，随时能从 git 重建，所以对不上只是提醒不算失败。
# 对得上说明 DDL/种子批次已经落库。
SEED_CHECKS: tuple[SampleCount, ...] = (
    ("metric_definition", "SELECT COUNT(*) FROM metric_definition", 91),
    ("rule_config", "SELECT COUNT(*) FROM rule_config", 66),
)

EXPECTED_PDFS = 16


def main() -> int:
    parser = argparse.ArgumentParser(description="数据库自检：确认不可再生的数据还在")
    parser.add_argument(
        "--db",
        default=None,
        help="要检查的库；默认 backend/var/finance.db。用于测试本脚本本身能抓出空库",
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DB_PATH

    if not db_path.exists():
        print(f"✗ 数据库不存在：{db_path}", file=sys.stderr)
        print("  建库：python scripts/init_db.py --force", file=sys.stderr)
        print("  导数据：python scripts/merge_data_pack.py --source <数据包>/backend/var/finance.db",
              file=sys.stderr)
        return 1

    print(f"数据库自检 — {db_path}")
    print()

    # 连接一律经由 connect()：它在每条连接上开外键并自检，失败即抛。
    # 直接 sqlite3.connect() 拿到的是外键关闭的连接（见 app/db/session.py）。
    con = connect(db_path)
    try:
        failed = _run_section(con, "数据完整性（不可再生，精确比对）", DATA_CHECKS, hard=True)
        print()
        failed |= _run_section(con, "派生视图", VIEW_CHECKS, hard=True)
        print()
        _run_section(con, "种子（来自 git，可重建；不符仅提醒）", SEED_CHECKS, hard=False)
    finally:
        con.close()

    print()
    pdfs = sorted(SAMPLES_DIR.rglob("*.pdf")) if SAMPLES_DIR.exists() else []
    if len(pdfs) == EXPECTED_PDFS:
        print(f"  ✓ 年报 PDF {len(pdfs)} 份")
    else:
        print(f"  ✗ 年报 PDF {len(pdfs)} 份，期望 {EXPECTED_PDFS} 份 — 见 {SAMPLES_DIR}")
        failed = True

    print()
    if failed:
        print("✗ 数据不完整。库多半被重建过而没重新导入数据包：")
        print("    python scripts/merge_data_pack.py --source <数据包>/backend/var/finance.db")
        return 1

    print("✓ 数据完整。")
    return 0


def _run_section(
    con: sqlite3.Connection,
    title: str,
    checks: tuple[SampleCount, ...],
    *,
    hard: bool,
) -> bool:
    """跑一组检查。返回是否有**硬失败**（hard=False 的组永远返回 False）。"""
    print(title)
    failed = False
    for label, sql, expected in checks:
        try:
            actual = con.execute(sql).fetchone()[0]
        except sqlite3.OperationalError as exc:
            # 表/视图不存在——多半是 schema 没建，或者本脚本期望的视图被删了。
            # 这不是「数据不对」而是「库结构不对」，同样要拦住。
            print(f"  ✗ {label:<24} 查询失败：{exc}")
            failed = True
            continue

        if actual == expected:
            print(f"  ✓ {label:<24} {actual}")
        elif hard:
            print(f"  ✗ {label:<24} {actual}，期望 {expected}")
            failed = True
        else:
            print(f"  · {label:<24} {actual}，期望 {expected}（提醒，不算失败）")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
