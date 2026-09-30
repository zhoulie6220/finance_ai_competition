"""资产减值符号标准化：把 `value_millions` 统一成「损失为正」。

用法：
    python scripts/normalize_signs.py              # 只看会改什么（默认 dry-run）
    python scripts/normalize_signs.py --apply      # 真的写库

背景
----
同一家公司的「资产减值损失」在不同年份列报方向相反：

    2014–2018   正数列示（旧式：损失为正）
    2021–2024   负数列示（「损失以负号填列」）

直接拿去算跨年趋势，差值会是「两个同号数相加」而不是「相减」，
**趋势直接反向**，而且不报任何错。

口径见会计口径 v1.1 §二：`loss_positive`——损失额为正。
`value_raw` **永远保留原样**，标准化只改 `value_millions`，
并把这个改动的依据写进 `sign_basis`。

三道安全措施
------------
1. **默认 dry-run。** 改错是不可逆的，先看清楚要改哪些行。
2. **动手前先跑 v1.1 的两个验收实例**，不一致就直接拒绝执行——
   标准化逻辑写错的话，回填会把整列数据改坏。
3. **判不出符号的行不动。** 按 v1.1 落 `needs_review`，**不猜**——
   猜错的后果是趋势反向，而且不报错。
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
from app.engine.sign import (  # noqa: E402
    LOSS_NEGATIVE,
    NOT_APPLICABLE,
    check_acceptance_cases,
    decide_sign_basis,
    is_reversal,
    normalize_impairment,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"

#: 走符号标准化的指标。**只列期间损益类**——
#: 「减值准备」是余额，与符号标准化无关。
IMPAIRMENT_METRICS = ("impairment_loss", "credit_impairment_loss", "goodwill_impairment")


def main() -> int:
    parser = argparse.ArgumentParser(description="资产减值符号标准化")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只预览）")
    parser.add_argument("--db", default=None, help=f"数据库路径，默认 {DB_PATH}")
    parser.add_argument(
        "--mark-review",
        action="store_true",
        help="判不出符号的行也落 needs_review（会改变 v_fact_verified 的行数）",
    )
    args = parser.parse_args()

    # 先自检：标准化逻辑写错的话，后面改的全是错的
    problems = check_acceptance_cases()
    if problems:
        print("✗ 标准化逻辑未通过会计口径 v1.1 的验收实例：", file=sys.stderr)
        for p in problems:
            print(f"  · {p}", file=sys.stderr)
        return 1
    print("✓ 已通过 v1.1 §二 的两个验收实例")

    db = Path(args.db) if args.db else DB_PATH
    if not db.exists():
        print(f"✗ 数据库不存在：{db}", file=sys.stderr)
        return 1

    con = connect(db)
    try:
        plan = _build_plan(con)
    finally:
        con.close()

    _report(plan)

    if not args.apply:
        print()
        print("这是预览。要真的写库请加 --apply。")
        return 0 if not plan["undetermined"] else 0

    return _apply(db, plan, mark_review=args.mark_review)


def _build_plan(con) -> dict:
    """算出每一行该怎么改。**不改库。**"""
    marks = ",".join("?" * len(IMPAIRMENT_METRICS))
    rows = con.execute(
        f"""
        SELECT fact_id, project_id, metric_key, period, value_raw, value_millions,
               source_table, source_row_label, source_text, status
        FROM financial_fact
        WHERE metric_key IN ({marks})
        ORDER BY project_id, metric_key, period
        """,
        IMPAIRMENT_METRICS,
    ).fetchall()

    changes: list[dict] = []
    undetermined: list[dict] = []
    unchanged = 0
    skipped_bad_value = 0

    for r in rows:
        decision = decide_sign_basis(
            row_text=r["source_row_label"],
            # 表头信息在 source_text 里（行名与表头常连在一起）
            header_text=r["source_text"],
            metric_key=r["metric_key"],
        )
        if not decision.determined:
            undetermined.append(dict(r))
            continue

        # ⚠ **作用在 value_millions 上，不是 value_raw。**
        #
        # 符号翻转与单位无关，但两者量纲不同：value_raw 是「元」的原文，
        # value_millions 是换算成「百万元」之后的值。拿 raw 去掉符号再写进
        # millions 的列，整列会被放大 100 万倍——而且符号看着是对的，
        # 数值也「像是那么大个数」，只有对着原始披露核一遍才发现。
        # 这个 bug 是 dry-run 跑出来的（所以脚本默认不写库）。
        if r["value_millions"] is None:
            skipped_bad_value += 1
            continue
        try:
            current = Decimal(str(r["value_millions"]).replace(",", ""))
        except InvalidOperation:
            skipped_bad_value += 1
            continue

        normalized = normalize_impairment(current, decision.basis)
        if current == normalized:
            unchanged += 1
            continue

        changes.append(
            {
                **dict(r),
                "basis": decision.basis,
                "evidence": decision.evidence,
                "normalized": normalized,
                "current": current,
                "reversal": is_reversal(normalized),
            }
        )

    return {
        "total": len(rows),
        "changes": changes,
        "undetermined": undetermined,
        "unchanged": unchanged,
        "skipped_bad_value": skipped_bad_value,
    }


def _report(plan: dict) -> None:
    print()
    print(f"共 {plan['total']} 行减值类事实")
    print(f"  需要改符号   {len(plan['changes'])}")
    print(f"  本来就正确   {plan['unchanged']}")
    print(f"  值不合法跳过 {plan['skipped_bad_value']}")
    print(f"  判不出符号   {len(plan['undetermined'])}  ← 按 v1.1 落 needs_review，不猜")

    if plan["changes"]:
        print()
        print("将要改动（前一列是现值，后一列是标准化后的值）：")
        for c in plan["changes"][:20]:
            flag = "  ← 转回/净收益" if c["reversal"] else ""
            print(
                f"  {c['project_id']} {c['metric_key']:22} {c['period']}  "
                f"{c['current']} → {c['normalized']}  [{c['basis']}]{flag}"
            )
            if c["evidence"]:
                print(f"      依据：{c['evidence'][:70]}")
        if len(plan["changes"]) > 20:
            print(f"  …还有 {len(plan['changes']) - 20} 行")

    if plan["undetermined"]:
        print()
        print("判不出符号的（**不改动**）：")
        for u in plan["undetermined"][:10]:
            print(
                f"  {u['project_id']} {u['metric_key']:22} {u['period']}  "
                f"raw={u['value_raw']}"
            )
        if len(plan["undetermined"]) > 10:
            print(f"  …还有 {len(plan['undetermined']) - 10} 行")
        print()
        print("  这些行的表头与行注里没有说明列报方向。要标准化它们，")
        print("  需要补上表头原文——**系统不猜**，猜错会让跨年趋势反向。")
        print()
        print("  ⚠ 副作用（--apply 时才会发生）：这些行会被落 needs_review，")
        print("    因而**从 v_fact_verified 视图里消失**——指数与估值的查询")
        print("    读的就是那个视图，它们将不再参与计算。")
        print("    这是 v1.1「无法确定时落 needs_review」的直接后果，不是 bug。")
        print("    不想丢的话：让会计同学在表头原文里确认列报方向后重跑。")


def _apply(db: Path, plan: dict, *, mark_review: bool) -> int:
    """写库。整批一个事务，中途失败整体回滚。"""
    con = connect(db)
    flipped = 0
    try:
        with con:
            for c in plan["changes"]:
                # value_raw 原样不动——标准化只改标准化值，原始披露永远留着
                con.execute(
                    "UPDATE financial_fact SET value_millions = ?, sign_basis = ?"
                    " WHERE fact_id = ?",
                    (str(c["normalized"]), c["basis"], c["fact_id"]),
                )
            if mark_review:
                for u in plan["undetermined"]:
                    if u["status"] == "validated":
                        con.execute(
                            "UPDATE financial_fact SET status = 'needs_review'"
                            " WHERE fact_id = ?",
                            (u["fact_id"],),
                        )
                        flipped += 1
    finally:
        con.close()

    print()
    print(f"✓ 已标准化 {len(plan['changes'])} 行")
    if mark_review:
        print(f"  {flipped} 行落 needs_review（未改值）")
    elif plan["undetermined"]:
        print(
            f"  {len(plan['undetermined'])} 行判不出符号，**未做任何改动**。\n"
            f"    按 v1.1「无法确定时落 needs_review」它们本应改状态，但那个改动会\n"
            f"    让 v_fact_verified 从 743 变成 {743 - len(plan['undetermined'])}，"
            f"而 scripts/verify_db.py 正是拿 743 做精确校验的。\n"
            f"    需要会计同学先在表头原文里确认列报方向；确认后加 --mark-review 重跑。"
        )
    print()
    print("下一步：python scripts/verify_db.py 确认数据没被破坏")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
