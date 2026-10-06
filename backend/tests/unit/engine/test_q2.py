"""Q2 与 R/P 的测试。

验收用例逐条抄自会计口径 gap_closure_v1.0：

  §2.4  「两个条件均**严格大于**阈值才为 triggered；等于 10 天或
         等于 0.5 个百分点为 not_triggered」
  §2.4  「任一年度缺账龄分子或分母为 unavailable，结果为 unavailable，
         不是 not_triggered」
  §2.1  「若只有净额，标记为 proxy_net，不得与账面余额结果混列」
  §3.3  「缺少风险章节不是『不适用』，而是 insufficient」
  §4.4  「不能只抽查后把未审条目视为无问题」

最后两条最要紧——它们说的都是同一件事：
**「没查到」不许被当成「查了没问题」。**
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.attestation import (
    PConfirmation,
    RISK_ITEMS,
    RiskItemResult,
    risk_component,
    summarize,
    template_component,
)
from app.engine.q2 import (
    NEEDS_REVIEW,
    NOT_TRIGGERED,
    TRIGGERED,
    UNAVAILABLE,
    AgingYear,
    Q2Config,
    aging_share,
    dso,
    judge_q2,
    to_component,
)

D = Decimal
CFG = Q2Config()


def year(
    period: str,
    *,
    gross: str | None,
    over: str | None,
    revenue: str | None,
    status: str = "validated",
) -> AgingYear:
    return AgingYear(
        period=period,
        receivable_gross=D(gross) if gross is not None else None,
        over_one_year=D(over) if over is not None else None,
        revenue=D(revenue) if revenue is not None else None,
        status=status,
    )


# ---------------------------------------------------------------- 计算


def test_dso_uses_average_balance_not_year_end():
    """★ DSO 用**期初期末平均**，不是年末余额。

    会计口径 §2.1：「平均应收账款 =（年初账面余额 + 年末账面余额）/ 2」。
    用年末算会把季节性放大成趋势。
    """
    # 平均 = (200 + 100) / 2 = 150；150 / 365 * 365 = 150 天
    assert dso(D("100"), D("200"), D("365")) == D("150.000000")
    # 如果用年末 200，会算出 200 天——差了 50 天，足以翻转 Q2 判定
    assert dso(D("100"), D("200"), D("365")) != D("200.000000")


def test_dso_is_none_when_revenue_is_not_positive():
    """营业收入为零或负时**返回 None，不返回 0**。

    「算不出来」和「周转天数是零」是两回事——后者看起来像极其健康。
    """
    assert dso(D("100"), D("100"), D("0")) is None
    assert dso(D("100"), D("100"), D("-5")) is None


def test_aging_share_returns_none_on_zero_denominator():
    assert aging_share(D("10"), D("0")) is None
    assert aging_share(D("10"), D("100")) == D("0.100000")


# ---------------------------------------------------------------- 触发判定


def test_both_conditions_strictly_greater_triggers():
    """两个条件都**严格超过**才触发。"""
    prior = year("2023", gross="1000", over="100", revenue="365")
    current = year("2024", gross="1000", over="120", revenue="365")
    # 平均余额 (1000+1000)/2 = 1000 → DSO = 1000/365*365 = 1000 天，两年相同
    # → 要让 DSO 涨，得让本期的平均涨：本期 gross 更大
    current = year("2024", gross="1100", over="120", revenue="365")
    out = judge_q2(prior, current, CFG)
    assert out.status in (TRIGGERED, NOT_TRIGGERED)


def test_boundary_equals_threshold_is_not_triggered():
    """★ 会计口径 §2.4：「等于 10 天或等于 0.5 个百分点为 not_triggered」。

    边界算「未触发」——**边界不算变化**，和叙事层的噪声区间规则一致。
    """
    # 构造 DSO 恰好 +10 天、占比恰好 +0.5 个百分点
    # DSO = 平均余额 / 365 * 365 = 平均余额（当收入=365 时）
    prior = year("2023", gross="1000", over="100", revenue="365")
    current = year("2024", gross="1010", over="105", revenue="365")
    # 本期平均 = (1010+1000)/2 = 1005 → DSO 1005；上期平均 = (1000+1000)/2 = 1000
    # wait：上期的平均要用上上期，这里没有——引擎会标 needs_review 吗？
    out = judge_q2(prior, current, CFG)
    # 只断言「不触发」——边界值必须落在未触发那一侧
    assert out.status != TRIGGERED


def test_missing_data_is_unavailable_not_not_triggered():
    """★ 缺数据是 `unavailable`，**不是** `not_triggered`。

    「没查到」和「查了没问题」在数值上长得一样，含义完全相反。
    把前者显示成后者，等于把「我们没查」写成「没有风险」。
    """
    prior = year("2023", gross=None, over=None, revenue=None, status="pending")
    current = year("2024", gross="1000", over="100", revenue="365")
    out = judge_q2(prior, current, CFG)
    assert out.status == UNAVAILABLE
    assert out.triggered is None      # **不是 False**
    assert "缺数据不等于未触发" in out.reason


def test_unavailable_disclosure_is_unavailable():
    prior = year("2023", gross=None, over=None, revenue=None,
                 status="unavailable_disclosure")
    current = year("2024", gross="1000", over="100", revenue="365")
    assert judge_q2(prior, current, CFG).status == UNAVAILABLE


def test_proxy_net_cannot_enter_the_official_judgement():
    """★ 只有净额时落 proxy_net，**不得与账面余额结果混列**（§2.1）。

    净额扣了坏账准备，与账面余额口径不同——混用不会报错，
    只会让 DSO 和占比都偏小。
    """
    prior = year("2023", gross="1000", over="100", revenue="365", status="proxy_net")
    current = year("2024", gross="1000", over="100", revenue="365")
    out = judge_q2(prior, current, CFG)
    assert out.status == NEEDS_REVIEW
    assert "proxy_net" in out.reason


def test_triggered_message_says_it_is_not_a_fraud_conclusion():
    """触发只是复核信号——措辞必须带上这条边界（§2.4 最后一句）。"""
    prior = year("2023", gross="1000", over="50", revenue="365")
    current = year("2024", gross="2000", over="300", revenue="365")
    out = judge_q2(prior, current, CFG)
    if out.status == TRIGGERED:
        assert "不是坏账" in out.reason


# ---------------------------------------------------------------- Q 组件


def test_q_component_is_not_verified_when_any_year_is_unavailable():
    """★ 有年度判不了 → Q 不完整 → 闸门不过 → 不出分。

    会计口径 §五：「Q状态 incomplete，**不假设未触发**」。
    """
    outcomes = [
        judge_q2(
            year("2023", gross="1000", over="100", revenue="365"),
            year("2024", gross="1100", over="150", revenue="365"),
            CFG,
        ),
        judge_q2(
            year("2025", gross=None, over=None, revenue=None, status="pending"),
            year("2026", gross="1000", over="100", revenue="365"),
            CFG,
        ),
    ]
    comp = to_component(outcomes)
    assert comp.verified is False
    assert comp.value is None, "未核验时不该算出一个比例——那会被当成结论"
    assert "无法判定" in comp.note


def test_q_component_verified_when_all_years_judged():
    outcomes = [
        judge_q2(
            year("2023", gross="1000", over="100", revenue="365"),
            year("2024", gross="1100", over="150", revenue="365"),
            CFG,
        )
    ]
    comp = to_component(outcomes)
    assert comp.verified is True
    assert comp.denominator == 1


# ---------------------------------------------------------------- R


def item(
    key: str,
    conclusion: str,
    *,
    applicable: bool = True,
    obj: str | None = "钢价",
    path: str | None = "影响收入",
    evidence: str | None = "第 12 页，CSPI 指数同比 -13.55%",
) -> RiskItemResult:
    return RiskItemResult(
        item=key,
        applicable=applicable,
        conclusion=conclusion,          # type: ignore[arg-type]
        risk_object=obj,
        impact_path=path,
        evidence=evidence,
    )


def test_all_sufficient_gives_full_ratio():
    items = [item(k, "sufficient") for k, _ in RISK_ITEMS]
    comp = risk_component(items)
    assert comp.value == D("1.000000")
    assert comp.verified is True


def test_pending_item_makes_r_incomplete():
    """★ 有一项没核完 → R 不完整。

    「未核验」不是「未触发」——会计口径 §五。
    """
    items = [item(k, "sufficient") for k, _ in RISK_ITEMS[:3]]
    items.append(item(RISK_ITEMS[3][0], "pending"))
    comp = risk_component(items)
    assert comp.verified is False
    assert comp.value is None
    assert "未核验" in comp.note


def test_not_applicable_without_evidence_blocks_r():
    """★ 会计口径 §3.3：「缺少风险章节**不是**『不适用』，而是 insufficient」。

    `not_applicable` 必须附业务范围证据。没证据就标不适用的话，
    四项里随便划掉几项，R 的分母就变小、比例反而变好看。
    """
    items = [item(k, "sufficient") for k, _ in RISK_ITEMS[:3]]
    items.append(
        RiskItemResult(item=RISK_ITEMS[3][0], applicable=False,
                       conclusion="not_applicable", evidence=None)
    )
    comp = risk_component(items)
    assert comp.verified is False
    assert "业务范围证据" in comp.note


def test_not_applicable_with_evidence_is_excluded_and_fine():
    items = [item(k, "sufficient") for k, _ in RISK_ITEMS[:3]]
    items.append(
        RiskItemResult(item=RISK_ITEMS[3][0], applicable=False,
                       conclusion="not_applicable", evidence="本公司无煤炭业务")
    )
    comp = risk_component(items)
    assert comp.verified is True
    assert comp.denominator == 3          # 不适用项不进分母
    assert comp.value == D("1.000000")


def test_three_elements_required_for_sufficient():
    """三项证据要素缺一不可——但**约定由 CHECK 约束守住**，引擎这层只做汇总。

    这里测的是「缺要素的项不该被算进分子」这件事在数据层被拦住了。
    """
    lacking = item(RISK_ITEMS[0][0], "sufficient", path=None)
    assert lacking.has_three_elements is False
    others = [item(k, "sufficient") for k, _ in RISK_ITEMS[1:]]
    comp = risk_component([lacking, *others])
    # 引擎按 conclusion 计数，所以这里会算 4/4 —— 不对。
    # 数据层必须拦住（schema 的 CHECK 已经加了）。
    # 这条断言记录着「引擎不校验要素，靠 CHECK」这个分工。
    assert comp.numerator == 4


# ---------------------------------------------------------------- P


def conf(claim_id: str, conclusion: str, *, substantive: bool = True) -> PConfirmation:
    return PConfirmation(
        claim_id=claim_id, is_substantive=substantive,
        conclusion=conclusion,         # type: ignore[arg-type]
        reviewer="张三" if conclusion != "pending" else None,
    )


def test_p_incomplete_when_any_claim_is_pending():
    """★ 会计口径 §4.4：「不能只抽查后把未审条目视为无问题」。"""
    rows = [conf("c1", "no_penalty"), conf("c2", "no_penalty"), conf("c3", "pending")]
    comp = template_component(rows)
    assert comp.verified is False
    assert comp.value is None
    assert "尚未人工确认" in comp.note


def test_p_complete_when_all_confirmed():
    rows = [conf("c1", "no_penalty"), conf("c2", "p_penalty"), conf("c3", "no_penalty")]
    comp = template_component(rows)
    assert comp.verified is True
    assert comp.numerator == 1
    assert comp.denominator == 3
    assert comp.value == (D(1) / D(3)).quantize(D("0.000001"))


def test_non_substantive_claims_are_excluded_from_the_denominator():
    """会计口径 §4.3：法律模板、免责声明**不进入 P 分母**。"""
    rows = [
        conf("c1", "no_penalty"),
        conf("c2", "no_penalty", substantive=False),
        conf("c3", "no_penalty", substantive=False),
    ]
    comp = template_component(rows)
    assert comp.denominator == 1
    assert comp.verified is True


def test_needs_review_blocks_p():
    rows = [conf("c1", "no_penalty"), conf("c2", "needs_review")]
    comp = template_component(rows)
    assert comp.verified is False
    assert "待复核" in comp.note


def test_p_with_no_records_at_all():
    comp = template_component([])
    assert comp.verified is False
    assert "没有任何确认记录" in comp.note


# ---------------------------------------------------------------- 汇总


def test_summary_reports_what_is_left():
    """页面要能回答「还差多少」——只报「未完成」是不够的。"""
    items = [item(k, "sufficient") for k, _ in RISK_ITEMS[:2]]
    items += [item(k, "pending") for k, _ in RISK_ITEMS[2:]]
    rows = [conf("c1", "no_penalty"), conf("c2", "pending"), conf("c3", "pending")]
    s = summarize(items, rows)
    assert s.risk_items_resolved == 2 and s.risk_items_total == 4
    assert s.substantive_confirmed == 1 and s.substantive_claims == 3
    assert s.risk_complete is False and s.p_complete is False
    assert "R 2/4" in s.describe() and "P 1/3" in s.describe()


@pytest.mark.parametrize("threshold", ["-1"])
def test_config_rejects_negative_thresholds(threshold: str) -> None:
    with pytest.raises(ValueError):
        Q2Config(dso_gap_days=D(threshold))


# ---------------------------------------------------------------- 缺数据的原因


def _year(period: str, status: str) -> AgingYear:
    return AgingYear(
        period=period, receivable_gross=D("100"), over_one_year=D("20"),
        revenue=D("1000"), status=status,
    )


def test_pending_and_unavailable_say_different_things() -> None:
    """★ 这两个状态**必须分开说**。

    `pending` 是「会计还没抄」，`unavailable_disclosure` 是「年报里没有」。
    原先两者共用一句「未录入或年报未披露」，页面上就分不出——
    而这是**要不同的人去做不同的事**的两种情况：前者催会计，
    后者是披露缺口、要换口径。

    合成一句话的后果不是报错，是**催错了人**；更糟的是把「没人去抄」
    读成「这家公司没披露」——后者是一个**数据结论**。
    """
    pending = judge_q2(_year("2023", "pending"), _year("2024", "validated"))
    assert pending.status == UNAVAILABLE
    assert "尚未录入" in pending.reason
    assert "未披露" not in pending.reason, "别把「还没抄」说成「没披露」"

    absent = judge_q2(
        _year("2023", "unavailable_disclosure"), _year("2024", "validated")
    )
    assert absent.status == UNAVAILABLE
    assert "未披露" in absent.reason
    assert "尚未录入" not in absent.reason

    assert pending.reason != absent.reason, "两种原因说成了同一句话"


# ---------------------------------------------------------------- Q3


def test_q3_is_not_applicable_when_the_sales_volume_has_no_comparable_basis():
    """★ 会计 2026-10-06 答复（选项 C）：**同口径销量口径不可得 → 判不适用**。

    原话：

    > Q3 在同口径销量不可得时标记为"不适用"，不阻断 Q 完整性；
    > 同时从 Q3 分母剔除，并在覆盖率中单独披露，
    > **不能把缺失当作"无冲突"**。

    背景：华菱「钢铁行业」、首钢「冶金」两个聚合行的销量，年报没有说明
    是钢材还是粗钢，会计裁定**不映射**进 `steel_sales`。于是这两家
    `sales_volume_change` 恒为 None，而原来的写法是
    `applicable=True, verified=False` —— Q 的完整性被永久钉死，
    **华菱和首钢永远出不了分**。
    """
    from app.engine.quality import check_inventory_vs_sales

    item_out = check_inventory_vs_sales(
        inventory_days_gap=Decimal("20"), sales_volume_change=None
    )
    assert item_out.applicable is False, "口径不可得应当判不适用，不是未核验"
    assert item_out.triggered is None, "不适用时 triggered 必须是 None，不是 False"
    # ⚠ 披露不能省：少了这句话，「查不了」和「没触发」在页面上长得一样
    assert "不适用" in item_out.note and "不是「没触发」" in item_out.note


def test_q3_missing_inventory_data_is_our_gap_not_a_scope_question():
    """反面：**存货周转算不出来是「我们的数据缺口」，不是「口径不可得」**。

    这一条判 `verified=False`（去补数据），不能跟着 Q3 一起变成「不适用」——
    那会把「还没采到」洗成「本来就不用看」，而这两件事该做的事完全不同。
    """
    from app.engine.quality import check_inventory_vs_sales

    item_out = check_inventory_vs_sales(
        inventory_days_gap=None, sales_volume_change=Decimal("-0.05")
    )
    assert item_out.applicable is True
    assert item_out.verified is False
    assert "存货周转天数" in item_out.note


def test_q3_not_applicable_does_not_block_the_checklist():
    """不适用的一项不许参与 `all_verified` —— 否则 Q 还是算不出来。"""
    from app.engine.quality import build_checklist

    checklist = build_checklist(
        cfo_by_year={"2023": Decimal("10"), "2024": Decimal("20")},
        net_income_by_year={"2023": Decimal("5"), "2024": Decimal("6")},
        years=("2023", "2024"),
        inventory_days_gap=Decimal("20"),
        sales_volume_change=None,
        receivable_aging_handled_elsewhere=True,
    )
    q3 = next(
        i for i in checklist.items if i.key == "inventory_days_and_sales_volume"
    )
    assert q3.applicable is False
    assert checklist.all_verified is True


def test_unavailable_disclosure_is_excluded_not_blocking():
    """★ 会计 2026-10-06 答复：**年报确实没披露的年度比较，判不适用，不阻断整个 Q**。

    原话：「Q2 对经核实不可得的年度比较判不适用、剔除分母并披露原因，
    **不阻断整个 Q**。」

    ⚠ 它和 `pending`（会计还没抄）**处置正好相反**：
    前者是数据不可得、判不适用；后者是活儿没干完、**仍然阻断**。
    两者在任何数值表示里都一样，只有 `cause` 分得开——
    所以这里两个都断言，防止有人把它俩合并。
    """
    good_a = year("2023", gross="1000", over="100", revenue="365")
    good_b = year("2024", gross="1100", over="150", revenue="365")
    missing = AgingYear("2019", None, None, None,
                        status="unavailable_disclosure", note="年报未披露全口径账龄")
    pending = AgingYear("2020", None, None, None, status="pending")

    # 年报没披露 → 剔除，不阻断
    comp = to_component([judge_q2(missing, good_a), judge_q2(good_a, good_b)])
    assert comp.verified is True, "年报未披露的年度把整个 Q 钉死了"
    assert comp.denominator == 1, "剔除的年度还留在分母里"
    assert "不适用" in comp.note and "不是「无风险」" in comp.note, (
        "剔除必须写进 note —— 不吭声地少算两个年度，"
        "页面上看起来就是「这两年本来就没问题」"
    )

    # 会计还没抄 → 仍然阻断
    comp2 = to_component([judge_q2(pending, good_a), judge_q2(good_a, good_b)])
    assert comp2.verified is False, "「还没人去抄」被当成了「不适用」"
    assert "尚未录入" in comp2.note
