"""财务事实的数据契约。

对外（API、前端、docs/01-data-contract.md）使用契约字段名 `metric` / `value`；
数据库列名是 `metric_key` / `value_millions`（更明确，避免「这个 value 的单位
到底是什么」的歧义）。两者的映射只发生在 repository 层，不要散落到业务代码里。

十个契约字段见 `FinancialFact` 的字段注释，`docs/01-data-contract.md` 是它们的
唯一定义源。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import (
    ExampleSource,
    FactStatus,
    IncomparableReason,
    ObservationResolution,
    PeriodKind,
    Scope,
    Severity,
    SignConvention,
    SourceLocation,
    Statement,
    UnitKind,
    ValueType,
)
from app.schemas.types import Money, Ratio


class MetricDefinition(BaseModel):
    """字段字典的一条。aliases / exclusion_terms 由会计同学维护。"""

    model_config = ConfigDict(title="字段字典条目", from_attributes=True)

    metric_key: str = Field(description="机器可读的指标键，如 'revenue'")
    label_cn: str
    aliases: list[str] = Field(default_factory=list, description="年报表格里的原始行名")
    exclusion_terms: list[str] = Field(
        default_factory=list,
        description="行名**精确等于**这些词时不得映射到本指标，用于挡住「营业成本率 → 营业成本」这类同数量纲的误映射",
    )
    statement: Statement
    value_type: ValueType
    unit_kind: UnitKind
    sign_convention: SignConvention
    is_nonrecurring: bool = False
    is_derived: bool = False
    industry: str | None = None
    parent_key: str | None = Field(default=None, description="「其中：」层级的父指标")
    example_sentence: str | None = None
    example_source: ExampleSource | None = Field(
        default=None,
        description="例句来源。synthetic_example 表示仍是占位符标准句，不得当作真实证据",
    )
    example_file: str | None = Field(
        default=None,
        description="例句所在的 PDF 文件名。example_source=annual_report 时必填",
    )
    example_page: int | None = Field(
        default=None,
        ge=1,
        description="例句所在页码。example_source=annual_report 时必填",
    )
    scope_note: str | None = None

    @model_validator(mode="after")
    def _example_provenance_is_complete(self) -> MetricDefinition:
        """标为年报原文就必须带出处（签字文档 §6）。

        与数据库的 CHECK 是同一条规则的两处表达——这里拦在构造期，能给出
        字段级的错误信息；数据库那处防的是绕过 Pydantic 的直接写入。
        """
        if self.example_source is ExampleSource.ANNUAL_REPORT:
            missing = [
                name
                for name, value in (
                    ("example_sentence", self.example_sentence),
                    ("example_file", self.example_file),
                    ("example_page", self.example_page),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "example_source=annual_report 必须同时提供例句与出处，缺少："
                    + "、".join(missing)
                )
        return self


class FinancialFact(BaseModel):
    """一条已验证或待复核的财务事实。

    十个契约字段：metric / value / unit / period / scope / source_file /
    source_page / source_text / confidence / status。
    """

    model_config = ConfigDict(title="财务事实", from_attributes=True)

    fact_id: str
    project_id: str
    company_id: str
    is_primary: bool

    # ---- 契约字段 ----
    metric: str = Field(description="契约名；数据库列是 metric_key")
    value: Money | None = Field(
        default=None,
        description="契约名；数据库列是 value_millions。单位统一为百万元。缺失时为 None，**绝不为 0**",
    )
    unit: str = "百万元"
    period: str = Field(description="'2024' / '2024H1' / '2024-12-31'")
    scope: Scope
    source_file: str
    source_page: int
    source_text: str
    confidence: float = Field(ge=0, le=1)
    status: FactStatus

    # ---- 口径与追溯 ----
    period_kind: PeriodKind
    period_start: str | None = None
    period_end: str | None = None
    # 原始披露值永不覆盖，保留可追溯的换算过程
    value_raw: str | None = None
    raw_unit: str | None = None
    unit_factor: str | None = None
    source_file_id: str
    source_printed_page: str | None = Field(
        default=None, description="页脚印刷页码；与 PDF 物理页序常不一致，必须分开存"
    )
    source_table: str | None = None
    source_row_label: str | None = Field(default=None, description="原始行名，保留原始标签")
    mapped_from: str | None = Field(
        default=None,
        description=(
            "来源映射（签字文档 A-2）：本行的值实际抽自哪一个字段。"
            "只披露「营业总收入」的年度，其值映射到 revenue 并记 mapped_from='total_revenue'。"
            "按 (metric, period) 聚合前必须看这一列——否则 revenue 与 total_revenue 会被重复计入"
        ),
    )
    bbox: str | None = None
    extractor: str = Field(description="'rule:v3' / 'llm:deepseek-chat@<prompt_hash>' / 'human:<uid>'")

    # ---- 重述：原值与重述值双行并存，永不 UPDATE 覆盖 ----
    restated: bool = False
    restatement_note: str | None = None

    # ---- 可比性 ----
    comparable: bool = True
    incomparable_reason: IncomparableReason | None = None

    created_at: str

    @model_validator(mode="after")
    def _enforce_hard_rules(self) -> FinancialFact:
        """与数据库 CHECK 约束同源的校验，让错误在入库前就暴露。

        数据库那一层是最后防线，不是唯一防线——在 API 层就拦下来，报错信息更清楚。
        """
        if self.status is FactStatus.VALIDATED:
            missing = [
                name
                for name, val in (
                    ("value", self.value),
                    ("source_text", self.source_text.strip() if self.source_text else ""),
                    ("source_page", self.source_page if self.source_page > 0 else None),
                    ("unit", self.unit),
                    ("period", self.period),
                    ("scope", self.scope),
                )
                if val in (None, "")
            ]
            if missing:
                raise ValueError(
                    f"无来源不得进已验证：status=validated 但缺少 {', '.join(missing)}"
                )

        if not self.comparable and self.incomparable_reason is None:
            raise ValueError("不可比必须写明原因（comparable=False 时 incomparable_reason 必填）")

        return self


class FactObservation(BaseModel):
    """同一笔事实在年报里的一次出现。

    同一个数字往往同时出现在主要指标表、三张主表、附注和正文里，精度还可能不同。
    每次出现各记一条观测，交叉校验后把被采纳的那条标为 adopted 并回填 fact_id，
    其余必须写明未采纳理由。
    """

    model_config = ConfigDict(title="事实观测来源", from_attributes=True)

    observation_id: str
    project_id: str
    company_id: str
    metric: str
    period: str
    period_kind: PeriodKind
    scope: Scope

    value_raw: str = Field(description="原样文本，如 '12,345,678,901.23'")
    raw_unit: str | None = None
    value: Money | None = None

    source_file_id: str
    source_page: int
    source_table: str | None = None
    source_location: SourceLocation
    source_text: str
    bbox: str | None = None

    confidence: float = Field(ge=0, le=1)
    extractor: str
    created_at: str

    resolution: ObservationResolution = ObservationResolution.PENDING
    resolved_fact_id: str | None = None
    rejection_note: str | None = None

    @model_validator(mode="after")
    def _enforce_resolution_rules(self) -> FactObservation:
        if self.resolution is ObservationResolution.ADOPTED and self.resolved_fact_id is None:
            raise ValueError("标为 adopted 必须回填 resolved_fact_id")
        if self.resolution is ObservationResolution.REJECTED and not (
            self.rejection_note or ""
        ).strip():
            raise ValueError("被否决的观测必须写明理由，禁止静默丢弃")
        if self.resolution is ObservationResolution.PENDING and self.resolved_fact_id is not None:
            raise ValueError("尚未裁决的观测不得提前绑定事实")
        return self


class FactCorrection(BaseModel):
    """人工修正。只追加增量，旧行保留，绝不原地覆盖。"""

    model_config = ConfigDict(title="人工修正记录", from_attributes=True)

    correction_id: str
    fact_id: str
    field: str
    old_value: str | None = None
    new_value: str | None = None
    reason: str
    operator: str
    created_at: str


class CheckResult(BaseModel):
    """一条勾稽/校验规则的执行结果。

    每条都带公式与输入 fact_id 列表，构成「结论 → 计算 → 字段 → 页码 → 原文」的证据链。
    """

    model_config = ConfigDict(title="校验结果", from_attributes=True)

    check_id: str
    project_id: str
    period: str
    scope: Scope
    rule_key: str = Field(description="如 'bs_equation' / 'ni_to_cfo_bridge' / 'cash_rollforward'")
    severity: Severity
    status: str
    lhs: Money | None = None
    rhs: Money | None = None
    diff: Money | None = None
    tolerance: str | None = None
    formula: str
    input_facts: list[str] = Field(default_factory=list, description="参与计算的 fact_id")
    message: str = Field(description="中文可读说明，页面直接展示")
    suggestion: str | None = None
    created_at: str
