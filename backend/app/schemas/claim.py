"""MD&A 可验证主张及其与财务事实的匹配。

系统只对具有明确主体、对象、期间和方向的主张做一致性分析。无法识别期间或对象的
泛化表述（「公司经营稳健」）标记为 background_only，只作背景展示，不进一致性评分。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import (
    ClaimDirection,
    ClaimType,
    FactStatus,
    MatchVerdict,
    MdnaSectionKind,
    Scope,
)
from app.schemas.types import Money, Ratio


class MdnaSection(BaseModel):
    """MD&A 章节切分结果。"""

    model_config = ConfigDict(title="MD&A 章节", from_attributes=True)

    section_id: str
    file_id: str
    heading: str | None = None
    kind: MdnaSectionKind
    page_from: int
    page_to: int
    text: str


class Claim(BaseModel):
    """一条管理层主张。

    `claim_text` 必须是**原句，不得改写**——证据链的终点就是这句话在年报里的位置。
    """

    model_config = ConfigDict(title="管理层主张", from_attributes=True)

    claim_id: str
    project_id: str
    section_id: str

    claim_text: str = Field(description="原句，不改写")
    subject: str | None = None
    action: str | None = None
    object: str | None = None
    period_expr: str | None = Field(default=None, description="原文期间表述，如「2024 年」")
    period_norm: str | None = Field(default=None, description="归一化期间")
    direction: ClaimDirection = ClaimDirection.UNKNOWN
    magnitude_text: str | None = Field(default=None, description="如「20% 以上」")
    magnitude_value: Money | None = None
    magnitude_unit: str | None = None

    claim_type: ClaimType
    verifiable: bool = Field(description="无法识别期间/对象时为 False")
    background_only: bool = Field(
        default=False, description="为 True 时仅作背景展示，不进入一致性评分"
    )
    confidence: float = Field(ge=0, le=1)

    source_file_id: str
    source_page: int
    source_text: str
    bbox: str | None = None

    extractor: str
    prompt_version: str
    llm_call_id: str | None = Field(
        default=None, description="抽取这条主张的那次模型调用，可回溯用的 prompt 原文"
    )
    status: FactStatus
    created_at: str

    @model_validator(mode="after")
    def _unverifiable_must_be_background(self) -> Claim:
        """不可验证的主张必须同时标为仅背景参考。

        两个字段语义重叠，如果允许「verifiable=False 但 background_only=False」，
        下游就可能把一句「公司经营稳健」拿去算一致性，得出无意义的分数。
        """
        if not self.verifiable and not self.background_only:
            raise ValueError(
                "无法识别期间或对象的主张必须标为 background_only，不得进入一致性评分"
            )
        return self


class ClaimIndicator(BaseModel):
    """一条主张对应的候选指标。一条主张可以有多个候选。"""

    model_config = ConfigDict(title="主张候选指标", from_attributes=True)

    id: str
    claim_id: str
    metric_key: str
    role: str = Field(description="'primary' 或 'supporting'")
    match_confidence: float = Field(ge=0, le=1)
    matched_by: str = Field(description="'rule:alias' / 'llm:<prompt>' / 'human:<uid>'，规则优先")


class ClaimMatch(BaseModel):
    """主张 × 指标 × 期间 三元组——诊断指数的基本观测单位。"""

    model_config = ConfigDict(title="主张—事实匹配", from_attributes=True)

    match_id: str
    claim_id: str
    metric_key: str
    fact_id: str | None = None
    claim_period: str
    fact_period: str

    direction_claim: ClaimDirection | None = None
    direction_actual: ClaimDirection | None = None
    direction_consistent: bool | None = None
    magnitude_target: str | None = None
    magnitude_actual: str | None = None
    relative_deviation: Ratio | None = None

    verdict: MatchVerdict
    reason: str = Field(description="中文理由，页面直接展示")
    confidence: float = Field(ge=0, le=1)
    formula: str | None = None
    inputs: dict[str, str] | None = Field(
        default=None, description="参与计算的 fact_id 与取值，保证可复算"
    )

    reviewer: str | None = None
    reviewed_at: str | None = None
    created_at: str

    @model_validator(mode="after")
    def _incomparable_requires_reason(self) -> ClaimMatch:
        if self.verdict is MatchVerdict.INCOMPARABLE and not (self.reason or "").strip():
            raise ValueError("判定为不可比时必须写明原因（并购/重述/季节性等）")
        return self


__all__ = ["MdnaSection", "Claim", "ClaimIndicator", "ClaimMatch"]
