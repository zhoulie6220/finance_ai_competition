"""周期正常化与估值的数据契约。

能源钢铁是强周期行业，**不能用最近一年的 FCFF 直接做永续增长**——周期高点上的
FCFF 因产品价格暴涨而虚高，直接永续增长等于把周期顶当常态。所以 DCF 的起点必须是
「穿越一个完整周期的中枢盈利能力」，即这里的 `NormalizationRun`。

核心口径：**收入加权的周期中位 EBIT margin**，主窗口 8 个完整年度，可比年度不足
7 个则扩展至 10 年，仍不足则拒绝出分。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import (
    AdjustmentCategory,
    AdjustmentDirection,
    AdjustmentReviewStatus,
    CyclePosition,
    EbitVariant,
    NormalizationStatus,
    ParamSourceType,
    PeerRole,
    Profitability,
    Scope,
    WindowMode,
)
from app.schemas.types import Money, Ratio


class EbitAdjustment(BaseModel):
    """一笔 EBIT 调整。每一笔都必须留下完整审计痕迹。"""

    model_config = ConfigDict(from_attributes=True)

    adjustment_id: str
    normalization_year_id: str
    item: str = Field(description="原始项目名称，原样保留年报里的写法")
    category: AdjustmentCategory
    amount: Money = Field(description="调整金额（正数）")
    direction: AdjustmentDirection
    reason: str = Field(description="调整理由，必须可读")

    source_file: str | None = None
    source_page: int | None = None

    review_status: AdjustmentReviewStatus = AdjustmentReviewStatus.PENDING
    reviewer: str | None = None
    reviewed_at: str | None = None
    created_at: str

    @model_validator(mode="after")
    def _reviewed_requires_reviewer(self) -> EbitAdjustment:
        if self.review_status is not AdjustmentReviewStatus.PENDING and not self.reviewer:
            raise ValueError("已裁决的调整必须记录复核人")
        return self


class NormalizationYear(BaseModel):
    """窗口内一年的明细。三种 EBIT 并存，禁止只留一个。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    normalization_id: str
    year: str

    # ---- 轻解析的原始输入（正常化只需要这几个）----
    revenue: Money | None = None
    total_profit: Money | None = None
    interest_expense: Money | None = None
    interest_income: Money | None = None
    operating_profit: Money | None = Field(default=None, description="交叉核对用")
    financial_expense: Money | None = Field(default=None, description="交叉核对用")

    # ---- 三种 EBIT ----
    reported_ebit: Money | None = None
    crosscheck_ebit: Money | None = None
    adjusted_ebit: Money | None = None
    crosscheck_deviation: Ratio | None = None
    adjustment_delta: Money | None = None
    adjustment_approved: bool = Field(
        default=False, description="会计是否已批准；未批准时主 DCF 只能用 reported"
    )

    # ---- 参与中枢计算的口径 ----
    ebit_margin: Ratio | None = None
    ebit_variant_used: EbitVariant | None = None
    revenue_weight: Ratio | None = None
    phase: str | None = Field(default=None, description="high / normal / low")
    is_median_year: bool | None = None

    # ---- 可比性：三步流程，不自动删除 ----
    comparable: bool = True
    incomparable_reason: str | None = None
    reviewed_by_accounting: bool = False
    included_in_median: bool = True

    revenue_fact_id: str | None = None
    ebit_fact_id: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _comparability_rules(self) -> NormalizationYear:
        if not self.comparable and not (self.incomparable_reason or "").strip():
            raise ValueError("不可比必须写明原因")
        if self.included_in_median and not self.comparable:
            raise ValueError("不可比年度不得进入主中枢（但整行保留，供敏感性分析使用）")
        return self


class NormalizationCrosscheck(BaseModel):
    """交叉验证项：吨钢毛利 / EBITDA margin / 产能利用率 / ROIC。

    **一律不影响 DCF。** 核心中枢只能来自 NormalizationRun.ebit_margin_mid。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    normalization_id: str
    metric_key: str
    variant: str = Field(default="primary", description="同一指标的多套口径分开存，禁止混用")
    mid_cycle_value: Money | Ratio | None = None
    unit: str | None = None
    basis: str | None = Field(default=None, description="原样记录数据来源口径")
    data_source: str = Field(description="company_disclosed / self_calculated / external")
    agrees_with_core: bool | None = None
    deviation_note: str | None = None
    affects_dcf: bool = Field(default=False, description="固定为 False")
    evidence_refs: dict[str, list[str]] | None = None
    created_at: str

    @model_validator(mode="after")
    def _never_affects_dcf(self) -> NormalizationCrosscheck:
        if self.affects_dcf:
            raise ValueError("交叉验证项不得影响 DCF——核心中枢只能来自 EBIT margin 收入加权中位")
        return self


class NormalizationRun(BaseModel):
    """一次周期正常化运行。"""

    model_config = ConfigDict(from_attributes=True)

    normalization_id: str
    project_id: str
    task_id: str | None = None

    # ---- 窗口 ----
    window_mode: WindowMode
    preferred_years: int = 8
    fallback_years: int = 10
    min_comparable_years: int
    anchor_year: str | None = Field(default=None, description="最新完整年度，窗口据此滚动")
    window_start: str | None = None
    window_end: str | None = None
    years_available: int | None = None
    comparable_years: int | None = None

    # ---- 高低盈利阶段覆盖校验 ----
    covers_high_phase: bool | None = None
    covers_low_phase: bool | None = None
    coverage_passed: bool | None = None
    high_threshold: Ratio | None = None
    low_threshold: Ratio | None = None
    coverage_detail: dict | None = None

    # ---- EBIT 口径 ----
    ebit_variant: EbitVariant = EbitVariant.REPORTED
    ebit_formula: str
    crosscheck_formula: str | None = None
    crosscheck_max_deviation: Ratio | None = None
    crosscheck_needs_review: bool = False

    # ---- 核心结果 ----
    core_metric: str = "ebit_margin"
    weighting: str = "revenue_weighted"
    ebit_margin_mid: Ratio | None = Field(
        default=None, description="收入加权的周期中位 EBIT margin。这是 DCF 的唯一起点"
    )
    ebit_margin_p25: Ratio | None = None
    ebit_margin_p75: Ratio | None = None

    status: NormalizationStatus
    insufficient_reason: str | None = None

    method_version: str
    rule_config_version: int
    years: list[NormalizationYear] = Field(default_factory=list)
    created_at: str

    @model_validator(mode="after")
    def _no_forced_normalization(self) -> NormalizationRun:
        """不强行正常化——失败时连一个「带警告的数字」都不给。

        如果这里返回了数字，哪怕附带警告，也一定会有调用方直接用上，
        正常化就形同虚设。下游 DCF 拿不到值就只能拒绝计算，这是刻意的。
        """
        ok = self.status in (
            NormalizationStatus.NORMALIZED,
            NormalizationStatus.EXTENDED_NORMALIZED,
        )
        if ok:
            if not self.coverage_passed:
                raise ValueError("未通过周期覆盖校验不得标记为已正常化")
            if self.ebit_margin_mid is None:
                raise ValueError("已正常化但缺少中枢值")
            if (
                self.comparable_years is not None
                and self.comparable_years < self.min_comparable_years
            ):
                raise ValueError("可比年度数不达标，不得标记为已正常化")
        else:
            if self.ebit_margin_mid is not None or self.ebit_margin_p25 is not None:
                raise ValueError("数据不足或周期不完整时不得输出任何中枢值")
            if not (self.insufficient_reason or "").strip():
                raise ValueError("必须写明无法正常化的原因，禁止静默降级")
        return self


class ComparableCompany(BaseModel):
    """可比公司。

    煤价是钢企的成本项，两者周期驱动因素相反。把煤企当作钢企的估值可比公司，
    中位数会失去意义——所以产业链上游公司只能以 chain_reference 身份出现。
    """

    model_config = ConfigDict(from_attributes=True)

    comparable_id: str
    project_id: str
    name: str
    stock_code: str | None = None
    industry: str
    sub_industry: str | None = None
    business_similarity: str
    scale_note: str | None = None
    growth_note: str | None = None
    profitability: Profitability
    data_date: str
    selection_reason: str = Field(description="选择理由必须记录")
    peer_role: PeerRole = PeerRole.VALUATION_PEER
    pe: Ratio | None = None
    ev_ebitda: Ratio | None = None
    pb: Ratio | None = None
    source: str
    excluded: bool = False
    exclude_reason: str | None = None


class ValuationParam(BaseModel):
    """一个估值参数。**每个参数都必须标记来源**，禁止模型凭记忆填入。"""

    model_config = ConfigDict(from_attributes=True)

    param_id: str
    scenario_id: str
    year: int | None = None
    name: str
    value: str
    source_type: ParamSourceType
    source_ref: str = Field(
        description="fact_id / comparable_id / 'user:<uid>' / 'assumption:<key>'"
    )


class ValuationScenario(BaseModel):
    """一个估值情景。输出永远是区间，不是单一目标价。"""

    model_config = ConfigDict(from_attributes=True)

    scenario_id: str
    run_id: str
    scenario: str = Field(description="base / bull / bear")
    weight: Ratio = Field(description="由诊断指数的映射规则给出，非手填")
    enterprise_value: Money | None = None
    equity_value: Money | None = None
    value_per_share: Money | None = None
    param_delta: dict = Field(default_factory=dict)
    trigger_refs: list[str] = Field(
        default_factory=list, description="引用 diagnosis_component.id，构成「原参数—新参数—触发证据」"
    )
    params: list[ValuationParam] = Field(default_factory=list)
    created_at: str


class ValuationRun(BaseModel):
    """一次估值运行。"""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    project_id: str
    task_id: str | None = None
    diagnosis_run_id: str | None = Field(
        default=None, description="指数→情景的传导来源"
    )

    model: str = "dcf_fcff"
    currency: str = "CNY"
    forecast_years: int
    base_year: str
    tax_rate: Ratio

    net_debt: Money
    net_debt_source: str
    non_operating_assets: Money | None = None
    minority_interest: Money | None = None
    lease_liability: Money | None = None
    diluted_shares: str = Field(description="EV → 股权价值必需")
    diluted_shares_source: str

    terminal_method: str = "gordon"
    cycle_position: CyclePosition = Field(
        default=CyclePosition.UNKNOWN,
        description="必须显式标注当前处于周期什么位置，不能假装周期不存在",
    )
    cycle_position_basis: str | None = None
    normalized_basis_id: str | None = Field(
        default=None,
        description="周期行业必填，且被引用那次运行必须是已正常化状态；否则 DCF 必须拒绝计算",
    )
    ebit_basis: EbitVariant = Field(
        default=EbitVariant.REPORTED,
        description="默认 reported；只有会计逐笔批准后才允许切到 adjusted",
    )

    exit_multiple_check: Ratio | None = None
    implied_exit_multiple: Ratio | None = None

    scenarios: list[ValuationScenario] = Field(default_factory=list)
    rule_config_version: int
    created_at: str
