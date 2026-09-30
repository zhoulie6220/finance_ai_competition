"""单位换算系数去浮点污染。

用法：
    python scripts/normalize_unit_factors.py            # 只看会改什么
    python scripts/normalize_unit_factors.py --apply    # 真的写库

问题
----
`unit_factor` 里存的是用 float 算出来的系数：

    9.999999999789054644501822963E-7       ← 应该是 0.000001
    0.000001000000000022912287026779358

实测 743 条里 351 条带这种噪声。

**数值本身没错**——`value_millions` 已经量化到 6 位小数，误差在 1e-6 量级，
不影响任何计算。但有两件事让人不舒服：

1. 页面上显示「换算系数 9.999999999789054644501822963E-7」，
   看的人有理由怀疑这套系统用了浮点——而本项目的规矩是**金额一律用 Decimal**。
2. 它确实是浮点污染的痕迹。留着它，等于默许这类误差再出现。

所以把系数改成**精确值**。`value_millions` 一个字都不动——
它本来就是对的，重算反而可能引入新的舍入。
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# _console 与本文件同目录。**这一行不能省**：
# 直接 `python scripts/x.py` 时 Python 会自动把脚本目录放进 sys.path，
# 但测试用 `spec_from_file_location` 按路径加载脚本时**不会**——
# 少了它，`import _console` 只在跑测试时炸，看起来像测试坏了。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402  (与本文件同目录)

_console.setup()

from app.db.session import connect  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"

#: 原始单位 → 目标单位（百万元）的**精确**换算系数。
#: 用 Decimal 从字符串构造，不用 float 运算——1/1000000 在 float 下
#: 就是 9.999999999789054644501822963E-7，这正是要修的东西。
EXACT_FACTORS: dict[tuple[str, str], str] = {
    ("元", "百万元"): "0.000001",
    ("千元", "百万元"): "0.001",
    ("万元", "百万元"): "0.01",
    ("百万元", "百万元"): "1",
    ("亿元", "百万元"): "100",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="单位换算系数去浮点污染")
    parser.add_argument("--apply", action="store_true", help="真的写库")
    parser.add_argument("--db", default=None, help=f"数据库路径，默认 {DB_PATH}")
    args = parser.parse_args()

    db = Path(args.db) if args.db else DB_PATH
    if not db.exists():
        print(f"✗ 数据库不存在：{db}", file=sys.stderr)
        return 1

    con = connect(db)
    try:
        plan, unknown = _build_plan(con)
        if args.apply and plan:
            with con:
                for fact_id, exact in plan:
                    con.execute(
                        "UPDATE financial_fact SET unit_factor = ? WHERE fact_id = ?",
                        (exact, fact_id),
                    )
    finally:
        con.close()

    print(f"需要修正 {len(plan)} 行")
    if unknown:
        print()
        print("⚠ 这些 (原始单位 → 单位) 组合不在已知表里，**跳过**：")
        for combo, n in sorted(unknown.items()):
            print(f"  {combo}  {n} 条")
        print("  换算系数不能猜——猜错会让金额整体差几个数量级，而且不报错。")

    if plan and not args.apply:
        print()
        print("样例：")
        for fact_id, exact in plan[:5]:
            print(f"  {fact_id}  →  {exact}")
        print()
        print("这是预览。要真的写库请加 --apply。")
        return 0

    if not plan:
        print("✓ 全部已是精确值，无需改动")
        return 0

    print()
    print(f"✓ 已修正 {len(plan)} 行的换算系数（value_millions 未改动）")
    print("  下一步：python scripts/verify_db.py")
    return 0


def _build_plan(con) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """找出所有系数不精确的行。**不改库。**"""
    rows = con.execute(
        """
        SELECT fact_id, raw_unit, unit, unit_factor
        FROM financial_fact
        WHERE unit_factor IS NOT NULL
        """,
    ).fetchall()

    plan: list[tuple[str, str]] = []
    unknown: dict[str, int] = {}

    for r in rows:
        combo = (r["raw_unit"] or "", r["unit"] or "")
        exact = EXACT_FACTORS.get(combo)
        if exact is None:
            unknown[f"{combo[0]} → {combo[1]}"] = unknown.get(f"{combo[0]} → {combo[1]}", 0) + 1
            continue

        stored = r["unit_factor"] or ""
        try:
            if Decimal(stored) == Decimal(exact):
                continue          # 已经精确
        except InvalidOperation:
            pass                  # 解析不了，也当成要修

        plan.append((r["fact_id"], exact))

    return plan, unknown


if __name__ == "__main__":
    raise SystemExit(main())
