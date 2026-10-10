"""受限历史计划兑现度：口径、闸门与「不声明就一点都不变」。

会计口径见《首钢出分问题解决方案》第二节。这一支的六条纳入条件里，
两条最容易漏，各有一组用例：

  · **过渡年排除**——预算是在合并范围还没变的时候编的，与目标年的实际不是
    同一个口径。首钢 2015 年报里的 2016 年预算就属此列。
  · **不套噪声带**——实际比预算低 0.3% 也算未达成。

还有一组守边界的：**没有声明开关的项目必须一个字都不变**。
`MatchConfig.budget_attainment` 默认 False，没有它时这条支路根本进不去。
"""

from __future__ import annotations

from decimal import Decimal

from app.engine.claim_match import (
    ActualValue,
    ClaimInput,
    MatchConfig,
    judge,
)

D = Decimal


def claim(**kw) -> ClaimInput:
    """一条首钢式的预算主张：2019 年报里写的 2020 年营业收入 716.4 亿元。"""
    base = dict(
        claim_id="cl-budget",
        claim_text="营业收入716.4亿元，同比增长3.57%。",
        claim_type="management_budget",
        direction="up",
        period_norm="2020",
        magnitude_value=D("716.4"),
        magnitude_unit="亿元",
        magnitude_raw="716.4亿元",
        bound="exact",
        primary_metric="revenue",
        metric_unit_kind="currency",
        metric_sign="positive_is_good",
        # 2020 年那一句是从 **2019 年报**里抽出来的——报告年是 2019。
        report_period="2019",
    )
    base.update(kw)
    return ClaimInput(**base)


def actual(value: str, period: str = "2020") -> ActualValue:
    return ActualValue(
        metric_key="revenue", period=period, value=D(value), fact_id=f"ff-{period}"
    )


ON = MatchConfig(scope_break_year="2015", budget_attainment=True)


# ---------------------------------------------------------------- 开关


def test_switch_off_means_this_path_is_never_taken():
    """★ 没声明 `narrative.budget_attainment.<项目>` 的项目**一个字都不变**。

    宝钢、华菱的 H 各自有 2 条观测，走的是原有路径；这条口径要是漏了出去，
    它们的脸色会变——而「宝钢 36.439610 / 华菱 35.272540 逐字不变」是
    方案文档第八节第 7 条明写的验收条件。
    """
    out = judge(claim(), current=actual("79951.181948"), base=None, cfg=MatchConfig())
    assert "受限历史计划兑现度" not in out.reason
    assert out.verdict != "supported" or "偏差" not in out.reason


def test_other_claim_types_are_untouched_even_with_the_switch_on():
    """开关开着，但这一条不是预算主张——照旧走普通判定。"""
    out = judge(
        claim(claim_type="demand"),
        current=actual("79951.181948"),
        base=None,
        cfg=ON,
    )
    assert "受限历史计划兑现度" not in out.reason


# ---------------------------------------------------------------- 达成 / 未达成


def test_attainment_when_actual_meets_the_budget():
    # 预算 716.4 亿元 = 71,640 百万元；实际 79,951.18 —— 达标
    out = judge(claim(), current=actual("79951.181948"), base=None, cfg=ON)
    assert out.verdict == "supported"
    assert out.scores
    assert out.magnitude_target == "716.4亿元"
    assert out.magnitude_actual == "79951.181948"
    assert out.target_millions == D("71640.0")
    assert out.unit_factor == D("100")
    assert out.plan_reference == "受限预算兑现度：达成"
    # 预算值、实际值、偏差金额、偏差率、来源页码——口径要求页面全部看得到
    for piece in ("71640.0", "79951.181948", "+8311.181948", "%", "合并范围"):
        assert piece in out.reason, piece


def test_miss_ignores_the_noise_band():
    """★ 实际比预算低 0.3% 也判未达成——**明确数值目标不许套统一容差**。

    这是这一支最容易被"看不下去"的地方：690.5 亿的目标做到 688.41 亿，
    人眼几乎分不出。但口径是「差得少不等于达标」，所以理由里必须把
    偏差原样写出来，让看的人知道它是照什么判的。
    """
    out = judge(
        claim(
            claim_text="营业收入690.5亿元，同比增长4.97%。",
            period_norm="2019",
            magnitude_value=D("690.5"),
            magnitude_raw="690.5亿元",
            report_period="2018",
        ),
        current=actual("68841.307822", "2019"),
        base=None,
        cfg=ON,
    )
    assert out.verdict == "contradicted"
    assert out.plan_reference == "受限预算兑现度：未达成"
    assert "不套噪声带" in out.reason
    assert "-208.692178" in out.reason


def test_plan_variance_stays_null():
    """★ 走的是 supported / contradicted，所以**绝不能**写 `plan_variance`。

    那一列有 CHECK：`plan_variance IS NULL OR verdict = 'needs_review'`。
    写进去 INSERT 会被数据库拒绝——这是刻意加的钉子，防的是
    「有人把 8-3 第 4 条挡住的计划偏差接进计分」。
    """
    for value in ("79951.181948", "118142.183549"):
        out = judge(claim(), current=actual(value), base=None, cfg=ON)
        assert out.plan_variance is None


# ---------------------------------------------------------------- 过渡年


def test_transition_year_is_excluded_not_scored():
    """★ 首钢 2015 年报里编的 2016 年预算——编制时还没并京唐，**同范围不成立**。

    必须 `needs_review`（不计分），而不是拿它去和 2016 年的实际比。
    比出来的东西有数字有理由，**唯独口径不是同一个**。
    """
    out = judge(
        claim(
            claim_text="2016年营业收入327.01亿元，同比下降11.9%。",
            period_norm="2016",
            magnitude_value=D("327.01"),
            magnitude_raw="327.01亿元",
            report_period="2015",
        ),
        current=actual("41850.407993", "2016"),
        base=None,
        cfg=ON,
    )
    assert out.verdict == "needs_review"
    assert not out.scores
    assert "断点" in out.reason
    assert "京唐" in out.reason
    # 留在覆盖率分母里——它是「还没核清」，不是「不可验证」
    assert out.in_denominator


def test_the_year_after_the_breakpoint_is_scored_normally():
    """2016 年报编的 2017 年预算就正常了：报告年 2016 > 断点 2015。"""
    out = judge(
        claim(
            claim_text="2017年营业收入448.5亿元，同比增加30亿元，增幅7.2%。",
            period_norm="2017",
            magnitude_value=D("448.5"),
            magnitude_raw="448.5亿元",
            report_period="2016",
        ),
        current=actual("60250.154291", "2017"),
        base=None,
        cfg=ON,
    )
    assert out.verdict == "supported"


def test_no_scope_break_declared_still_judges():
    """没声明断点的项目，条件 4 不适用——照判，不因为"不知道"就挡掉。"""
    out = judge(
        claim(), current=actual("79951.181948"), base=None,
        cfg=MatchConfig(budget_attainment=True),
    )
    assert out.verdict == "supported"


# ---------------------------------------------------------------- 其余闸门


def test_target_year_without_actuals_is_unverifiable():
    """目标年还没数据（未到期或未采集）→ 不可验证，**不进覆盖率分母**。

    用 0 或「未达成」代替会是一条凭空的冲突——v1.1 反复禁的就是这个。
    """
    out = judge(claim(period_norm="2026"), current=None, base=None, cfg=ON)
    assert out.verdict == "unverifiable"
    assert not out.in_denominator
    assert "未到验证期" in out.reason


def test_number_not_taken_from_the_metric_alias_is_not_compared():
    """退回取到的「第一个带单位的数」不与主判据比，**连偏差都不算**。

    实测撞到过「预算安排固定资产投资资金239.2亿元」被当成年营业成本目标。
    """
    out = judge(
        claim(target_metric_aligned=False),
        current=actual("79951.181948"), base=None, cfg=ON,
    )
    assert out.verdict == "needs_review"
    assert "没有和主判据" in out.reason


def test_dimension_mismatch_is_refused_before_any_arithmetic():
    """量纲对不上**连偏差都不算**——「703 吨」减不掉「79951 百万元」。

    ⚠ 这条闸门挡在换算之前，是刻意的：两个不同量纲的数相减，
    偏差算得出来、理由写得出，**和真结论长得一模一样**。
    """
    out = judge(
        claim(magnitude_unit="吨", magnitude_value=D("703")),
        current=actual("79951.181948"), base=None, cfg=ON,
    )
    assert out.verdict == "needs_review"
    assert "不能相减" in out.reason
    assert out.relative_deviation is None


def test_unit_without_an_official_factor_is_refused():
    """单位不在 8-1 给的金额类里、又不是吨 → 量纲那道闸门够不着它，
    所以必须在换算这一步挡掉——**不猜一个看起来合理的换算**。"""
    out = judge(
        claim(magnitude_unit="倍", magnitude_raw="716.4倍", magnitude_value=D("716.4")),
        current=actual("79951.181948"), base=None, cfg=ON,
    )
    assert out.verdict == "needs_review"
    assert "换算依据" in out.reason


def test_every_budget_verdict_is_one_of_three():
    """预算主张只会落进三种：计分两态 + 不计分的三种之一。"""
    cases = [
        (actual("79951.181948"), "supported"),
        (actual("118142.183549"), "supported"),
        (actual("68841.307822"), "contradicted"),
        (None, "unverifiable"),
    ]
    for current, want in cases:
        out = judge(claim(), current=current, base=None, cfg=ON)
        assert out.verdict == want
