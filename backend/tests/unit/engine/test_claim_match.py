"""主张—事实判定的测试。

**判定顺序就是全部规格。** 同一个输入按不同顺序判会得出完全不同的结论，
而且大多不会报错。所以下面按 v1.1 §A.2/A.5 的顺序逐条测。

三个验收用例直接抄自会计口径 §A.9，逐字实现：

    毛利率 10.00% → 9.96%（降 0.04 个百分点）  → neutral，不是 contradicted
    收入 100 → 100.8（涨 0.8%）                 → neutral，不是 contradicted
    「增长至少 5%」实际 4%                       → contradicted，差得少也算未达标
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.claim_match import (
    ActualValue,
    ClaimInput,
    MatchConfig,
    RatioOutOfRange,
    classify_change,
    judge,
    metric_kind,
    summarize,
)

D = Decimal
CFG = MatchConfig()


def claim(**kw) -> ClaimInput:
    base = dict(
        claim_id="c1",
        claim_text="测试主张",
        claim_type="demand",
        direction="up",
        period_norm="2024",
        magnitude_value=None,
        magnitude_unit=None,
        primary_metric="steel_sales_volume",
    )
    base.update(kw)
    return ClaimInput(**base)


def actual(metric: str, period: str, value: str, **kw) -> ActualValue:
    return ActualValue(
        metric_key=metric, period=period, value=D(value),
        fact_id=f"f-{metric}-{period}", **kw,
    )


# ---------------------------------------------------------------- 三个验收用例


def test_v11_margin_004pp_is_neutral_not_contradicted() -> None:
    """★ v1.1 §A.9 验收用例。

    毛利率 10.00% → 9.96%，降 0.04 个百分点，落在 0.5 个百分点的噪声带内。
    主张是「毛利改善」，实际是降——**但不能判相悖**，要判无明显变化。
    """
    c = claim(direction="improve", primary_metric="gross_margin")
    out = judge(
        c,
        current=actual("gross_margin", "2024", "0.0996"),
        base=actual("gross_margin", "2023", "0.1000"),
        cfg=CFG,
    )
    assert out.verdict == "neutral", out.reason
    assert out.scores is True


def test_v11_margin_060pp_is_a_real_signal() -> None:
    """降 0.60 个百分点才越过噪声带，此时才判相悖。"""
    c = claim(direction="improve", primary_metric="gross_margin")
    out = judge(
        c,
        current=actual("gross_margin", "2024", "0.0940"),
        base=actual("gross_margin", "2023", "0.1000"),
        cfg=CFG,
    )
    assert out.verdict == "contradicted"


def test_v11_revenue_08pct_is_neutral_not_contradicted() -> None:
    """★ v1.1 §A.9 验收用例。

    收入 100 → 100.8，涨 0.8%，落在 1% 噪声带内。
    「增长」这个方向是对的，但幅度是噪声——判无明显变化，**不是相悖**。
    """
    c = claim(direction="up", primary_metric="revenue")
    out = judge(
        c,
        current=actual("revenue", "2024", "100.8"),
        base=actual("revenue", "2023", "100"),
        cfg=CFG,
    )
    assert out.verdict == "neutral", out.reason


def test_v11_explicit_target_missed_at_4pct_is_contradicted() -> None:
    """★ v1.1 §A.9 验收用例，也是最容易写错的一条。

    「收入增长至少 5%」实际增长 4%：只差 1 个百分点，落在 1% 噪声带之外、
    但人眼看上去非常接近。**必须判未达成。**

    数值目标不套用噪声带——目标是目标。差得少不等于达标。
    """
    c = claim(
        direction="up",
        primary_metric="revenue",
        magnitude_value=D("5"),
        magnitude_unit="%",
        bound="at_least",
        magnitude_raw="5%以上",
    )
    out = judge(
        c,
        current=actual("revenue", "2024", "104"),
        base=actual("revenue", "2023", "100"),
        cfg=CFG,
    )
    assert out.verdict == "contradicted", out.reason
    assert "未达成" in out.reason


def test_target_met_is_supported() -> None:
    c = claim(
        direction="up", primary_metric="revenue",
        magnitude_value=D("5"), magnitude_unit="%", bound="at_least",
        magnitude_raw="5%以上",
    )
    out = judge(
        c, current=actual("revenue", "2024", "107"),
        base=actual("revenue", "2023", "100"), cfg=CFG,
    )
    assert out.verdict == "supported"


def test_an_absolute_amount_target_is_never_silently_contradicted() -> None:
    """★ 绝对量目标**不能**和相对变化相比——这是算术上的必然，不是精度问题。

    宝钢 2018 年真实一句：「计划营业成本 2,420 亿元」。
    金额类指标的实测值是**相对变化**（(2,590.85 − 2,484.25) / 2,484.25 = 0.0429），
    拿 2,420 去减 0.0429，偏差必然是约 −2,420，**任何绝对量目标都判未达成**。

    实测宝钢一次跑出 12 条这样的「未达成」，进 H 的 9 个观测里有 7 个是这么来的。
    它们有公式、有偏差数字、有理由，和真结论长得一模一样。

    要真判得先换算单位（亿元 → 百万元）、再定「计划成本」的达成方向——
    **那是会计口径**。所以在定下来之前，一律转人工复核，不擅自判。
    """
    c = claim(
        direction="unknown",
        primary_metric="operating_cost",
        magnitude_value=D("2420"),
        magnitude_unit="亿元",
        bound="exact",
        magnitude_raw="2420亿元",
        is_plan=True,
    )
    out = judge(
        c,
        current=actual("operating_cost", "2018", "2590.85"),
        base=actual("operating_cost", "2017", "2484.25"),
        cfg=CFG,
    )
    assert out.verdict == "needs_review", out.reason
    assert "绝对量" in out.reason
    assert out.scores is False


def test_a_ratio_target_is_still_judged() -> None:
    """上一条的反面：**比例型**目标照常判，不能被那道闸门一并挡掉。

    只断言「绝对量不再判未达成」是不够的——把整条目标判定删掉也能通过。
    """
    c = claim(
        direction="up",
        primary_metric="revenue",
        magnitude_value=D("5"),
        magnitude_unit="%",
        bound="at_least",
        magnitude_raw="5%以上",
        is_plan=True,
    )
    out = judge(
        c,
        current=actual("revenue", "2024", "104"),
        base=actual("revenue", "2023", "100"),
        cfg=CFG,
    )
    assert out.verdict == "contradicted", out.reason
    assert "未达成" in out.reason


def test_a_reported_fact_is_not_a_target() -> None:
    """上一条的反面，也是**更要紧**的那一面。

    「2022 年公司销售商品坯材 4,976.3 万吨」是**报告**不是承诺。
    把它当目标核验，等于拿事实核验事实——永远判「支持」，
    而假的「支持」会把 H 和 C 一起抬上去，且从数字上看不出来。

    所以 `is_plan` 必须由「同一分句里有没有计划模态词」决定，
    不能靠「有没有数字」。
    """
    c = claim(
        direction="unknown",
        primary_metric="steel_sales_volume",
        magnitude_value=D("4976.3"),
        magnitude_unit="万吨",
        bound="exact",
        magnitude_raw="4,976.3万吨",
        is_plan=False,
    )
    out = judge(
        c,
        current=actual("steel_sales_volume", "2022", "4976.3"),
        base=actual("steel_sales_volume", "2021", "4650"),
        cfg=CFG,
    )
    assert out.verdict == "needs_review", out.reason
    assert "方向" in out.reason


def test_approximate_target_is_needs_review_not_a_verdict() -> None:
    """「约 10%」没有公开容差，只展示偏差，**不擅自认定完成或未完成**。"""
    c = claim(
        direction="up", primary_metric="revenue",
        magnitude_value=D("10"), magnitude_unit="%", bound="about",
        magnitude_raw="约10%",
    )
    out = judge(
        c, current=actual("revenue", "2024", "109"),
        base=actual("revenue", "2023", "100"), cfg=CFG,
    )
    assert out.verdict == "needs_review"
    assert out.scores is False


# ---------------------------------------------------------------- 判定顺序


def test_missing_primary_metric_is_unverifiable_not_flat() -> None:
    """★ 查不到 ≠ 没变化。

    把「查不到」当成「没变化」会凭空给一条主张记 0 分，而 0 分在中性证据
    那里恰好是「无异议」——等于把「我们没查到」读成了「一切正常」。
    """
    out = judge(claim(), current=None, base=None, cfg=CFG)
    assert out.verdict == "unverifiable"
    assert out.in_denominator is False
    assert "不降级" in out.reason


def test_theme_without_a_metric_is_unverifiable() -> None:
    """产品结构升级这类主判据字段字典里没有的，一律不可验证。"""
    out = judge(
        claim(primary_metric=None),
        current=actual("gross_margin", "2024", "0.1"),
        base=actual("gross_margin", "2023", "0.1"),
        cfg=CFG,
    )
    assert out.verdict == "unverifiable"


def test_incomparable_is_not_contradicted() -> None:
    """★ 不可比不能判成相悖。

    并购/重述/季节性导致的口径不可比，与「主张说错了」是两回事。
    判成相悖会凭空制造一个风险信号。
    """
    out = judge(
        claim(direction="up", primary_metric="revenue"),
        current=actual("revenue", "2024", "80", comparable=False,
                       incomparable_reason="mna"),
        base=actual("revenue", "2023", "100"),
        cfg=CFG,
    )
    assert out.verdict == "incomparable"
    assert "mna" in out.reason
    assert out.in_denominator is False


def test_needs_review_stays_in_the_denominator() -> None:
    """★ 待核查留在覆盖率分母。

    它只是**还没查完**，踢出分母会让覆盖率虚高——本来没查的东西
    变成了「不在统计范围内」。
    """
    out = judge(
        claim(direction="up", primary_metric="revenue"),
        current=actual("revenue", "2024", "110"),
        base=None,
        cfg=CFG,
    )
    assert out.verdict == "needs_review"
    assert out.in_denominator is True
    assert out.scores is False


# ---------------------------------------------------------------- 噪声带分派


@pytest.mark.parametrize(
    "metric,kind",
    [
        ("revenue", "amount"),
        ("steel_sales_volume", "amount"),
        ("gross_margin", "ratio"),
        ("roic", "ratio"),
        ("capacity_utilization", "utilization"),
    ],
)
def test_metric_kind_dispatch(metric: str, kind: str) -> None:
    assert metric_kind(metric) == kind


def test_amount_uses_relative_band_ratio_uses_absolute() -> None:
    """★ 金额用相对变化，比例用绝对变化。

    共用一个阈值会让某一类宽出两个数量级：金额上的绝对 0.005 太紧，
    比例上的相对 1% 太松。混用**不会报错**，只会让判定悄悄失真。
    """
    # 金额：100 → 100.5，相对 0.5% < 1% → flat
    assert classify_change("amount", D("100.5"), D("100"), CFG) == "flat"
    # 同一个数放在比例上：0.1000 → 0.1005，绝对 0.0005 < 0.005 → flat
    assert classify_change("ratio", D("0.1005"), D("0.1000"), CFG) == "flat"
    # 但金额上 100 → 102（相对 2%）就不是噪声了
    assert classify_change("amount", D("102"), D("100"), CFG) == "up"


def test_ratio_out_of_range_blows_up() -> None:
    """★ 比例若被存成百分数（10.00 而不是 0.10），必须炸掉。

    0.5 个百分点的噪声带会宽出 200 倍，于是**每一个比例类判定都落到
    「无明显变化」**——全部判定被静默中和，一个冲突都报不出来。
    那比崩掉糟得多：崩掉至少知道出事了。
    """
    with pytest.raises(RatioOutOfRange):
        classify_change("ratio", D("10.05"), D("10.00"), CFG)
    with pytest.raises(RatioOutOfRange):
        classify_change("ratio", D("0.1"), D("-0.1"), CFG)


def test_negative_base_amount_does_not_produce_a_direction() -> None:
    """负基期的「增长」没有经济意义，不能硬算一个百分比。"""
    assert classify_change("amount", D("50"), D("-100"), CFG) is None


def test_days_band() -> None:
    assert classify_change("days", D("103"), D("100"), CFG) == "flat"
    assert classify_change("days", D("104"), D("100"), CFG) == "up"


# ---------------------------------------------------------------- 方向语义


def test_improve_means_down_for_cost_metrics() -> None:
    """★ 「改善」对不同的指标含义相反。

    毛利率改善是上升，成本改善是下降。按词面统一处理会让一半的判定
    方向反过来，而且不报错。
    """
    cost = claim(direction="improve", primary_metric="operating_cost")
    down = judge(
        cost,
        current=actual("operating_cost", "2024", "90"),
        base=actual("operating_cost", "2023", "100"),
        cfg=CFG,
    )
    assert down.verdict == "supported"     # 成本降了 = 改善

    up = judge(
        cost,
        current=actual("operating_cost", "2024", "110"),
        base=actual("operating_cost", "2023", "100"),
        cfg=CFG,
    )
    assert up.verdict == "contradicted"    # 成本涨了 = 改善落空


def test_margin_improve_means_up() -> None:
    out = judge(
        claim(direction="improve", primary_metric="gross_margin"),
        current=actual("gross_margin", "2024", "0.12"),
        base=actual("gross_margin", "2023", "0.10"),
        cfg=CFG,
    )
    assert out.verdict == "supported"


def test_unknown_direction_goes_to_review() -> None:
    out = judge(
        claim(direction="unknown", primary_metric="revenue"),
        current=actual("revenue", "2024", "110"),
        base=actual("revenue", "2023", "100"),
        cfg=CFG,
    )
    assert out.verdict == "needs_review"


# ---------------------------------------------------------------- 禁止的简化


def test_supporting_metrics_never_enter_the_judgement() -> None:
    """★ 佐证指标不能制造冲突。

    v1.1 §A.6 点名禁止：「CFO 下滑不能单独否定回款」「整体毛利率不能单独
    证明或否定产品升级」。实现上的保证是**判定只看主判据**——
    佐证指标根本进不到 judge 里，所以那些简化推断在结构上就不可能发生。

    （比写一条「不要用 CFO 否定回款」的规则更可靠：规则会被绕过，
    结构不会。）
    """
    # 回款类主张的主判据是应收账款，CFO 无论如何变化都不参与
    c = claim(direction="improve", primary_metric="accounts_receivable")
    import inspect

    from app.engine import claim_match

    src = inspect.getsource(claim_match.judge)
    assert "cfo" not in src.lower(), "judge 里出现了 cfo——佐证指标混进判定了"


# ---------------------------------------------------------------- 统计口径


def test_summarize_defines_n_and_N() -> None:
    """N 与 n 的口径写在代码里，避免调用方各自解释。"""
    outcomes = (
        judge(claim(claim_id="a"), current=actual("steel_sales_volume", "2024", "110"),
              base=actual("steel_sales_volume", "2023", "100"), cfg=CFG),   # supported
        judge(claim(claim_id="b"), current=actual("steel_sales_volume", "2024", "100.5"),
              base=actual("steel_sales_volume", "2023", "100"), cfg=CFG),   # neutral
        judge(claim(claim_id="c"), current=None, base=None, cfg=CFG),       # unverifiable
        judge(claim(claim_id="d"), current=actual("steel_sales_volume", "2024", "110"),
              base=None, cfg=CFG),                                          # needs_review
    )
    s = summarize(outcomes)
    assert s["supported"] == 1 and s["neutral"] == 1
    assert s["unverifiable"] == 1 and s["needs_review"] == 1
    assert s["n"] == 2          # 只有计分态进分子
    assert s["N"] == 3          # 不可验证不进分母；待核查留在分母
