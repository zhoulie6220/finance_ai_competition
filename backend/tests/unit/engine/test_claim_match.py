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


def cost_target(**kw) -> ClaimInput:
    """宝钢 2018 年真实那一句的判定输入。

        2018年，宝钢股份计划产铁4563万吨、产钢4737万吨、销售商品坯材4568万吨、
        营业总收入2786亿元、营业成本2420亿元。

    实际营业成本 2,590.85 亿元 = 259,084.996 百万元（财务事实库的单位是百万元）。
    """
    base = dict(
        claim_id="cl-plan-cost-2018",
        claim_text="2018年，宝钢股份计划产铁4563万吨……营业成本2420亿元。",
        claim_type="cost",
        direction="unknown",
        period_norm="2018",
        magnitude_value=D("2420"),
        magnitude_unit="亿元",
        magnitude_raw="2420亿元",
        bound="exact",
        primary_metric="operating_cost",
        is_plan=True,
        metric_unit_kind="currency",
        metric_sign="negative_is_good",
    )
    base.update(kw)
    return ClaimInput(**base)


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

    8-1/8-2/8-3（2026-10-01）定下来之后，这里换成了「换算 + 报偏差，
    但不出计分结论」——**仍然是 needs_review，仍然不进 H**。
    """
    out = judge(
        cost_target(),
        current=actual("operating_cost", "2018", "259084.996015"),
        base=actual("operating_cost", "2017", "248425.102399"),
        cfg=CFG,
    )
    assert out.verdict == "needs_review", out.reason
    assert out.scores is False


# ---------------------------------------------------------------- 8-1 单位换算


def test_yuan_is_converted_to_millions_and_the_factor_is_kept() -> None:
    """★ 8-1：1 亿元 = 100 百万元，而且**四样都要留痕**。

    会计原话：「系统同时保留原始数值、原始单位、换算因子和标准化数值，
    避免把换算后的数值当成原始披露」。

    原始数值与原始单位在 claim 表上；这里断言换算因子与标准化值
    确实带在返回值里——不然页面上只剩一个换算过的数，看不出它从哪来。
    """
    out = judge(
        cost_target(),
        current=actual("operating_cost", "2018", "259084.996015"),
        base=None,
        cfg=CFG,
    )
    assert out.target_unit == "亿元"
    assert out.unit_factor == D("100")
    assert out.target_millions == D("242000"), "2,420 亿元应是 242,000 百万元"
    assert "242000" in out.formula and "× 100" in out.formula


def test_the_deviation_is_the_real_baosteel_gap() -> None:
    """偏差必须是**真数**：2,590.85 亿 − 2,420 亿 = 170.85 亿元 = 17,084.996 百万元。

    这个数落在 8-3 第 4 条允许展示的「原始计划偏差」上——
    它是这条链路唯一该被人看到的数字，所以钉死它。
    """
    out = judge(
        cost_target(),
        current=actual("operating_cost", "2018", "259084.996015"),
        base=None,
        cfg=CFG,
    )
    assert out.plan_variance == D("17084.996015")
    assert out.relative_deviation is not None
    # 17084.996 / 242000 = 7.06%
    assert abs(out.relative_deviation - D("0.070599")) < D("0.000001")


def test_a_unit_that_has_no_agreed_conversion_is_refused() -> None:
    """★ 8-1 只授权了**金额**的换算，吨 / 万吨没给。

    事实库里存的是吨还是万吨没定，所以这里**不猜一个看起来合理的因子**。
    反例是必要的：把 `_MILLION_FACTOR` 写成「不认识的单位就按 1 算」
    会让这条测试红，而线上表现是「万吨目标被当成百万元」——差四个数量级，
    且不报错。
    """
    out = judge(
        cost_target(magnitude_value=D("30"), magnitude_unit="万吨",
                    magnitude_raw="30万吨", metric_unit_kind="ton",
                    metric_sign="positive_is_good", primary_metric="steel_sales_volume"),
        current=actual("steel_sales_volume", "2018", "300000"),
        base=None,
        cfg=CFG,
    )
    assert out.verdict == "needs_review", out.reason
    assert out.unit_factor is None, "不认识的单位不许给换算因子"
    assert out.plan_variance is None, "没换算就不能算偏差"
    assert "换算依据" in out.reason


# ---------------------------------------------------------------- 8-2 达成方向


def test_a_cost_target_above_plan_reads_as_missed() -> None:
    """8-2：成本类，实际**高于**计划 → 参考结论「未达成」。"""
    out = judge(
        cost_target(),
        current=actual("operating_cost", "2018", "259084.996015"),
        base=None,
        cfg=CFG,
    )
    assert out.plan_reference is not None
    assert "未达成" in out.plan_reference
    assert "成本类" in out.plan_reference


def test_a_cost_target_below_plan_reads_as_met() -> None:
    """8-2 的反面。只测「高于计划 → 未达成」是不够的——
    把方向写死成「未达成」也能通过。"""
    out = judge(
        cost_target(),
        current=actual("operating_cost", "2018", "230000"),
        base=None,
        cfg=CFG,
    )
    assert "达成" in out.plan_reference
    assert "未达成" not in out.plan_reference
    assert out.plan_variance is not None and out.plan_variance < 0


def test_revenue_direction_is_the_opposite_of_cost() -> None:
    """★ 8-2 的**方向是反的**，这一点最容易写错。

        成本类：实际 ≤ 计划 为达成（花得比计划少 = 好）
        收入类：实际 ≥ 计划 为达成（挣得比计划多 = 好）

    两处用同一个比较符的话，一半的参考结论会反过来，**而且不报错**。
    """
    # 同一句里还写着「营业总收入2786亿元」，实际 2,700 亿元 → 收入类判未达成
    revenue = dict(
        primary_metric="total_revenue", metric_sign="positive_is_good",
        magnitude_value=D("2786"), magnitude_raw="2786亿元",
    )
    below = judge(
        cost_target(**revenue),
        current=actual("total_revenue", "2018", "270000"),   # 2,700 亿 < 计划 2,786 亿
        base=None,
        cfg=CFG,
    )
    assert "未达成" in below.plan_reference, below.plan_reference
    assert "收入 / 收益类" in below.plan_reference

    above = judge(
        cost_target(**revenue),
        current=actual("total_revenue", "2018", "290000"),
        base=None,
        cfg=CFG,
    )
    assert "未达成" not in above.plan_reference


def test_an_unknown_sign_convention_does_not_invent_a_direction() -> None:
    """sign_convention 是 `neutral` 时方向不明——**不判**，只报偏差。

    拿「越大越好」当默认值的话，应付账款、存货这类中性科目会被
    按一个没人认过的方向判出参考结论。
    """
    out = judge(
        cost_target(metric_sign="neutral"),
        current=actual("operating_cost", "2018", "259084.996015"),
        base=None,
        cfg=CFG,
    )
    assert out.plan_variance is not None, "偏差还是要报"
    assert "方向判不了" in out.plan_reference


def test_an_approximate_absolute_target_is_flagged_not_scored() -> None:
    """8-2 末句：「约 / 左右」先标待核查，**不擅自套用固定容差**。"""
    out = judge(
        cost_target(bound="about", magnitude_raw="约2420亿元"),
        current=actual("operating_cost", "2018", "259084.996015"),
        base=None,
        cfg=CFG,
    )
    assert out.verdict == "needs_review"
    assert "待核查" in out.plan_reference
    assert "未达成" not in out.plan_reference


# ---------------------------------------------------------------- 8-3 产量调整


def test_the_plan_variance_never_enters_h_or_c() -> None:
    """★★ 8-3 第 4 条：**未经调整的总额差异不进 H 的支持/相悖判定。**

    这是整条链路最要紧的一条断言。这一批主张的 verdict 永远只能是
    needs_review，`scores` 永远 False——所以它们既不进 n，也不改 H / C。
    会计答复的原话是「不应仅用 2,590.85 亿元对 2,420 亿元直接判定未达成；
    应先完成同口径、产量和成本结构复核」。
    """
    out = judge(
        cost_target(),
        current=actual("operating_cost", "2018", "259084.996015"),
        base=None,
        cfg=CFG,
    )
    assert out.scores is False
    assert out.verdict not in ("supported", "neutral", "contradicted")
    # 8-3 第 3 条：必须说清为什么还不判，而不是含糊地「待核查」
    assert "成本结构" in out.reason
    assert "产量" in out.reason
    # 留在覆盖率分母：它是「还没查完」，不是「不在范围内」
    assert out.in_denominator is True


def test_a_number_not_next_to_the_metric_is_refused_before_any_arithmetic() -> None:
    """★ 这条挡的是**比量纲更上游**的错误：那个数根本不是这个指标的数。

    真实撞到过 5 条：

        2024年，公司预算安排固定资产投资资金239.2亿元，主要用于……

    主题映射成 `operating_cost`，239.2 亿元其实是资本开支。拿它和
    营业成本（2,809 亿元）比，偏差 +280,626 百万元——按 8-2 的成本方向
    算出来还是「未达成」，**有数字、有理由、有公式**。

    量纲那道闸门挡不住它（两边都是金额），所以这里**连偏差都不算**。
    """
    out = judge(
        cost_target(target_metric_aligned=False,
                    magnitude_value=D("239.2"), magnitude_raw="239.2亿元"),
        current=actual("operating_cost", "2024", "280936.36"),
        base=None,
        cfg=CFG,
    )
    assert out.verdict == "needs_review"
    assert out.plan_variance is None, "连偏差都不许算——算出来就会有人当真"
    assert out.target_millions is None
    assert out.plan_reference is None
    assert "没有和主判据" in out.reason


def test_a_unit_mismatch_is_refused() -> None:
    """兜底：目标写「亿元」而主判据是「钢材销量（吨）」——两个数不能相减。

    这是真实数据里的错配（「营业总收入较计划减少 298.46 亿元」被映射到
    钢材销量上），金额与吨的偏差算得出来也毫无意义。
    """
    out = judge(
        cost_target(primary_metric="steel_sales_volume", metric_unit_kind="ton",
                    metric_sign="positive_is_good"),
        current=actual("steel_sales_volume", "2015", "2000"),
        base=None,
        cfg=CFG,
    )
    assert out.verdict == "needs_review"
    assert out.plan_variance is None
    assert "量纲" in out.reason and "不能相减" in out.reason


def test_an_unknown_metric_unit_kind_does_not_block() -> None:
    """★ 反面：**「没查」不等于「对不上」。**

    主判据量纲缺（字典里没这个指标、或调用方没传）时不能报错——
    那会把一整类主张静默降级成待核查。只有两边都知道且确实不同才挡。
    """
    out = judge(
        cost_target(metric_unit_kind=None),
        current=actual("operating_cost", "2018", "259084.996015"),
        base=None,
        cfg=CFG,
    )
    assert out.target_millions == D("242000"), "量纲未知时仍应换算"
    assert out.plan_variance is not None


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


def test_improve_means_up_for_volume_metrics() -> None:
    """★ 「销量改善」＝销量**上升**。

    `_IMPROVING_IS_UP` **不在表里的一律按「改善 = 下降」**——那个默认值对
    成本、费用、应收、天数是对的，漏掉「销量」就整个反掉：
    `extract_direction("销量改善")` 得到 improve，映射成 down，
    而销量实际是上升的，判出来是「相悖」，理由是
    「steel_sales_volume 的实际变化为 up，方向相反」。**看着完全正常。**

    实测宝钢 2023 年报「实现钢产能的有效发挥」那一类就是这么被判反的。
    """
    for metric in ("steel_sales_volume", "revenue", "cfo"):
        up = judge(
            claim(direction="improve", primary_metric=metric),
            current=actual(metric, "2024", "110"),
            base=actual(metric, "2023", "100"),
            cfg=CFG,
        )
        assert up.verdict == "supported", f"{metric}：改善应当算上升"


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
