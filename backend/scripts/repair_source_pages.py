"""修正 `financial_fact.source_page` 的页码偏差。

用法：
    python scripts/repair_source_pages.py            # 只看会改什么
    python scripts/repair_source_pages.py --apply    # 真的写库

## 问题

实测 743 条事实里，**只有 55.6% 记的页码是对的**：

    页码正确        413  (55.6%)
    在下一页        277  (37.3%)
    在下两页         53  ( 7.1%)
    找不到            0

`source_page` 是数据契约的**十个必填字段之一**——它不准，
「点结论回到计算过程」这条链路就有一半是不可靠的：
点开抽屉，看到的是一页不相干的原文。

## 判据是「找得到」，不是「猜」

只在**确实能在某一页里找到这段原文**时才改，而且范围限制在 ±2 页内。
找不到的**一律不动**——超出这个范围说明是别的问题（比如换了一份年报），
那时候改页码只会把错误藏得更深。

去空白后比对：年报原文里常有「财务费用  (五)56」这种多空格，
而 `source_text` 是解析时另存的，空格未必一致。

## 为什么是改数据而不是改查询

也可以让查询端每次都去搜 ±2 页。但那样：
  · `source_page` 这个契约字段**永远错着**，而它是要交给评审看的
  · 每查一次就多搜 4 页，而这是一个每格都要查的网格页
  · 别的消费方（导出、报告）读到的还是错的页码

所以修数据，一次修对。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import connect  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"

#: 往前/往后找几页。实测偏差不超过 2 页；范围再放大就失去了「确定性」——
#: 偏差 5 页的话，多半是换了年报或者口径不同，那时候不该改页码。
SEARCH_RANGE = 2

#: 太短的片段到处都能匹配上，那种「找到」没有意义。
MIN_SNIPPET = 6


def main() -> int:
    parser = argparse.ArgumentParser(description="修正事实的 source_page 偏差")
    parser.add_argument("--apply", action="store_true", help="真的写库")
    parser.add_argument("--db", default=None, help=f"数据库路径，默认 {DB_PATH}")
    args = parser.parse_args()

    db = Path(args.db) if args.db else DB_PATH
    if not db.exists():
        print(f"✗ 数据库不存在：{db}", file=sys.stderr)
        return 1

    con = connect(db)
    try:
        plan, stats = _build_plan(con)
        if args.apply and plan:
            with con:
                for fact_id, new_page in plan:
                    con.execute(
                        "UPDATE financial_fact SET source_page = ? WHERE fact_id = ?",
                        (new_page, fact_id),
                    )
    finally:
        con.close()

    _report(plan, stats)

    if not args.apply:
        print()
        print("这是预览。要真的写库请加 --apply。")
        return 0

    print()
    print(f"✓ 已修正 {len(plan)} 条事实的页码")
    print("  下一步：python scripts/verify_db.py 确认数据没被破坏")
    return 0


def _build_plan(con) -> tuple[list[tuple[str, int]], dict]:
    """找出页码不对的行，算出正确页码。**不改库。**"""
    rows = con.execute(
        "SELECT fact_id, source_file_id, source_page, source_text"
        " FROM financial_fact WHERE source_text IS NOT NULL"
    ).fetchall()

    # 一次把用到的页读进内存——逐条查会是几千次往返
    pages: dict[tuple[str, int], str] = {}
    for fid, pno, text in con.execute(
        "SELECT file_id, page_no, text FROM document_page"
    ):
        pages[(fid, pno)] = "".join((text or "").split())

    plan: list[tuple[str, int]] = []
    stats: Counter = Counter()
    examples: list[str] = []

    for r in rows:
        snippet = "".join((r["source_text"] or "").split())
        if len(snippet) < MIN_SNIPPET:
            stats["片段太短，跳过"] += 1
            continue

        base = r["source_page"]
        fid = r["source_file_id"]

        found: int | None = None
        for delta in range(0, SEARCH_RANGE + 1):
            for candidate in ({base - delta, base + delta} if delta else {base}):
                if snippet in pages.get((fid, candidate), ""):
                    found = candidate
                    break
            if found is not None:
                break

        if found is None:
            stats["±2 页内找不到"] += 1
            continue
        if found == base:
            stats["本来就对"] += 1
            continue

        plan.append((r["fact_id"], found))
        stats[f"修正 {found - base:+d} 页"] += 1
        if len(examples) < 5:
            examples.append(
                f"  {r['fact_id']}  {base} → {found}"
                f"   「{r['source_text'][:36]}」"
            )

    return plan, {**stats, "examples": examples, "total": len(rows)}


def _report(plan: list, stats: dict) -> None:
    print(f"共 {stats['total']} 条事实")
    print()
    for key in sorted(k for k in stats if k not in ("examples", "total")):
        print(f"  {key:<16} {stats[key]}")
    if stats.get("examples"):
        print()
        print("样例：")
        for e in stats["examples"]:
            print(e)


if __name__ == "__main__":
    raise SystemExit(main())
