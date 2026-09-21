"""数据契约测试。

重点不在「字段能不能填」，而在**不该通过的东西必须过不去**。这些校验和
schema.sql 里的 CHECK 约束是同源的：数据库那层是最后防线，不是唯一防线——
在 API 层就拦下来，报错信息更清楚，也不必等到写库才发现。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas import (
    EbitAdjustment,
    FactObservation,
    FinancialFact,
    NormalizationCrosscheck,
    NormalizationRun,
    NormalizationYear,
    Scope,
)
from app.schemas.enums import (
    AdjustmentCategory,
    AdjustmentDirection,
    AdjustmentReviewStatus,
    FactStatus,
    IncomparableReason,
    NormalizationStatus,
    ObservationResolution,
    PeriodKind,
    SourceLocation,
    WindowMode,
)


def fact(**over) -> FinancialFact:
    base = dict(
        fact_id="k1", project_id="p1", company_id="c1", is_primary=True,
        metric="revenue", value="12345678901.23", unit="百万元", period="2024",
        scope=Scope.CONSOLIDATED, source_file="2024年年度报告.pdf", source_page=86,
        source_text="营业收入 12,345,678,901.23", confidence=0.96,
        status=FactStatus.VALIDATED, period_kind=PeriodKind.CURRENT,
        source_file_id="f1", extractor="rule:v1", created_at="2026-09-21T00:00:00",
    )
    base.update(over)
    return FinancialFact(**base)


# ---------------------------------------------------------------- 金额精度


def test_money_serializes_as_string_to_preserve_precision():
    """JSON number 是双精度，14 位金额往返一次就会失真。出网必须是字符串。"""
    raw = fact().model_dump_json()
    assert '"value":"12345678901.23"' in raw


def test_float_input_is_converted_via_str():
    """Decimal(0.1) 会带一长串二进制尾数；必须走 str 中转。"""
    f = fact(value=0.1)
    assert f.value == Decimal("0.1")


# ---------------------------------------------------------------- 硬规则一


@pytest.mark.parametrize(
    "override",
    [
        {"source_text": "   "},
        {"source_page": 0},
        {"value": None},
    ],
)
def test_validated_fact_requires_full_source(override):
    with pytest.raises(ValidationError, match="无来源不得进已验证"):
        fact(**override)


def test_needs_review_may_lack_source():
    """待复核的数据本就允许来源不全——这正是它存在的意义。"""
    f = fact(status=FactStatus.NEEDS_REVIEW, source_text="", source_page=0)
    assert f.status is FactStatus.NEEDS_REVIEW


# ---------------------------------------------------------------- 硬规则三


def test_incomparable_requires_reason():
    with pytest.raises(ValidationError, match="不可比必须写明原因"):
        fact(comparable=False, incomparable_reason=None)


def test_incomparable_with_reason_is_accepted():
    f = fact(comparable=False, incomparable_reason=IncomparableReason.MNA)
    assert f.incomparable_reason is IncomparableReason.MNA


# ---------------------------------------------------------------- 观测裁决


def obs(**over):
    base = dict(
        observation_id="o1", project_id="p1", company_id="c1", metric="revenue",
        period="2024", period_kind=PeriodKind.CURRENT, scope=Scope.CONSOLIDATED,
        value_raw="12,345,678,901.23", source_file_id="f1", source_page=86,
        source_location=SourceLocation.MAIN_STATEMENT, source_text="营业收入",
        confidence=0.9, extractor="rule:v1", created_at="2026-09-21T00:00:00",
    )
    base.update(over)
    return FactObservation(**base)


def test_adopted_observation_must_bind_a_fact():
    with pytest.raises(ValidationError, match="必须回填"):
        obs(resolution=ObservationResolution.ADOPTED)


def test_rejected_observation_must_explain():
    with pytest.raises(ValidationError, match="必须写明理由"):
        obs(resolution=ObservationResolution.REJECTED)


def test_pending_observation_cannot_bind_fact_early():
    with pytest.raises(ValidationError, match="不得提前绑定"):
        obs(resolution=ObservationResolution.PENDING, resolved_fact_id="k1")


# ---------------------------------------------------------------- 不强行正常化


def norm(**over) -> NormalizationRun:
    base = dict(
        normalization_id="n1", project_id="p1", window_mode=WindowMode.PRIMARY_8Y,
        min_comparable_years=7, status=NormalizationStatus.INSUFFICIENT_DATA,
        ebit_formula="利润总额 + 利息费用 − 利息收入",
        method_version="normalization:v1", rule_config_version=1,
        created_at="2026-09-21T00:00:00",
    )
    base.update(over)
    return NormalizationRun(**base)


def test_failure_must_not_carry_a_central_value():
    """失败时连「带警告的数字」都不给：给了就一定会被用上。"""
    with pytest.raises(ValidationError, match="不得输出任何中枢值"):
        norm(ebit_margin_mid="0.074", insufficient_reason="仅 5 个完整年度")


def test_failure_must_explain_itself():
    with pytest.raises(ValidationError, match="必须写明"):
        norm(insufficient_reason="   ")


def test_success_requires_coverage_and_value():
    with pytest.raises(ValidationError, match="未通过周期覆盖校验"):
        norm(status=NormalizationStatus.NORMALIZED, coverage_passed=False,
             ebit_margin_mid="0.0742", comparable_years=8)

    with pytest.raises(ValidationError, match="缺少中枢值"):
        norm(status=NormalizationStatus.NORMALIZED, coverage_passed=True,
             comparable_years=8)

    with pytest.raises(ValidationError, match="可比年度数不达标"):
        norm(status=NormalizationStatus.NORMALIZED, coverage_passed=True,
             ebit_margin_mid="0.0742", comparable_years=5)


def test_valid_normalization_passes():
    n = norm(
        status=NormalizationStatus.NORMALIZED, coverage_passed=True,
        comparable_years=8, ebit_margin_mid="0.0742",
    )
    assert n.ebit_margin_mid == Decimal("0.0742")
    assert n.status is NormalizationStatus.NORMALIZED


def test_incomparable_year_cannot_enter_median():
    """不可比年度必须排除出中枢，但整行保留——敏感性分析还要用它。

    这里要写明 reason，否则会先撞上「不可比必须写明原因」那条，测不到本规则。
    """
    with pytest.raises(ValidationError, match="不得进入主中枢"):
        NormalizationYear(
            id="y1", normalization_id="n1", year="2019",
            comparable=False, incomparable_reason="mna", included_in_median=True,
        )
    # 排除出中枢则允许
    y = NormalizationYear(
        id="y2", normalization_id="n1", year="2019",
        comparable=False, incomparable_reason="mna", included_in_median=False,
        ebit_margin="0.60", revenue="1000",
    )
    assert y.ebit_margin == Decimal("0.60"), "数据本身仍保留，供敏感性分析使用"


# ---------------------------------------------------------------- 交叉验证不得影响 DCF


def test_crosscheck_can_never_affect_dcf():
    """吨钢毛利、产能利用率、ROIC 只做验证。核心中枢只能来自 EBIT margin。"""
    with pytest.raises(ValidationError, match="不得影响 DCF"):
        NormalizationCrosscheck(
            id="c1", normalization_id="n1", metric_key="roic",
            data_source="self_calculated", affects_dcf=True,
            created_at="2026-09-21T00:00:00",
        )


# ---------------------------------------------------------------- 调整需复核人


def test_decided_adjustment_requires_reviewer():
    with pytest.raises(ValidationError, match="必须记录复核人"):
        EbitAdjustment(
            adjustment_id="a1", normalization_year_id="y1", item="政府补助",
            category=AdjustmentCategory.GOV_SUBSIDY, amount="1200",
            direction=AdjustmentDirection.DEDUCT, reason="一次性补助，予以剔除",
            review_status=AdjustmentReviewStatus.APPROVED, reviewer=None,
            created_at="2026-09-21T00:00:00",
        )
