"""叙事—财务一致性诊断指数。

指数由**可解释规则**产生，不是黑箱打分：

    I = 50 + 20·H + 20·C + 5·R − 10·P − 15·Q      裁剪到 [0, 100]

    H 历史兑现度   C 当前一致性   R 风险披露变化
    P 模板化惩罚   Q 财务质量冲突

分母只含 supported / partial / conflicted 三种观测；incomparable 与 missing
既不计入分母也不扣分，只在页面单列。覆盖率不足时**不出分**（grade=insufficient），
只展示证据表与人工复核入口。

指数不得单独生成买卖指令，只能按公开映射规则影响估值情景的权重与参数范围。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import IndexGrade
from app.schemas.types import Ratio


class DiagnosisComponent(BaseModel):
    """指数的构成项。页面据此展示「这个分是怎么来的」。"""

    model_config = ConfigDict(title="诊断指数构成项", from_attributes=True)

    id: str
    run_id: str
    component: str = Field(
        description="history / current / risk_shift / template_penalty / quality_conflict"
    )
    raw_value: str | None = None
    weight: str | None = None
    contribution: str | None = None
    formula: str
    evidence_refs: dict[str, list[str]] = Field(
        default_factory=dict,
        description="触发本项的 claim_ids / fact_ids / check_ids / match_ids",
    )
    explanation: str = Field(description="中文说明，模板生成而非 LLM 生成")


class DiagnosisRun(BaseModel):
    """一次诊断指数计算的结果。"""

    model_config = ConfigDict(title="诊断指数结果", from_attributes=True)

    run_id: str
    project_id: str
    task_id: str | None = None
    rule_config_version: int = Field(description="引用规则版本 → 同输入可复现同结果")

    # ---- 观测统计 ----
    observation_count: int
    comparable_count: int = Field(
        description="进入分母的有效观测 = supported + partial + conflicted"
    )
    incomparable_count: int = Field(default=0, description="不扣分")
    missing_count: int = Field(default=0, description="不推断失败，转人工复核")
    partial_count: int = Field(default=0, description="方向一致但幅度偏弱")

    # ---- 结果 ----
    coverage: Ratio | None = Field(
        default=None, description="置信度加权覆盖率 = Σconf(有效观测) / Σconf(全部可验证主张)"
    )
    score: Ratio | None = Field(default=None, description="0–100；证据不足时为 None")
    grade: IndexGrade
    confidence: float | None = Field(default=None, ge=0, le=1)
    insufficient_reason: str | None = None
    conclusion_boundary: str = Field(
        description="审慎表述模板。禁止输出「管理层叙事虚假」等超出证据范围的结论"
    )

    components: list[DiagnosisComponent] = Field(default_factory=list)
    created_at: str

    @model_validator(mode="after")
    def _insufficient_must_not_score(self) -> DiagnosisRun:
        """证据不足时不得出分。

        如果这里放一个「带警告的数字」，界面一定会把它渲染成一个大号分数，
        人工复核入口就形同虚设。
        """
        if self.grade is IndexGrade.INSUFFICIENT:
            if self.score is not None:
                raise ValueError("grade=insufficient 时不得输出分值")
            if not (self.insufficient_reason or "").strip():
                raise ValueError("grade=insufficient 时必须写明原因")
        elif self.score is None:
            raise ValueError(f"grade={self.grade} 但未给出分值")
        return self


class RuleConfigItem(BaseModel):
    """一条规则参数。页面提供查看 / 修改 / 恢复默认。"""

    model_config = ConfigDict(title="规则参数", from_attributes=True)

    key: str
    industry: str = Field(default="", description="空串表示全局默认；填具体行业则只覆盖该行业")
    value: str
    value_type: str
    label_cn: str
    description: str
    unit: str | None = None
    default_value: str = Field(description="「恢复默认」的依据")
    min_value: str | None = None
    max_value: str | None = None


class ScenarioDelta(BaseModel):
    """诊断指数向估值情景的透明传导结果。

    **纯函数的产物，绝不产出目标价。** 每次传导都要能回答
    「原参数是什么、新参数是什么、由哪条证据触发、适用范围到哪」。
    """

    model_config = ConfigDict(title="指数向情景的传导")

    scenario: str = Field(description="base / bull / bear")
    weight: Ratio = Field(description="情景权重，由映射规则给出而非手填")
    revenue_growth_ref: str | None = Field(
        default=None, description="如 'historical_p25'，指向 rule_config 里的取数规则"
    )
    wacc_delta: Ratio | None = None
    terminal_growth_delta: Ratio | None = None
    trigger_refs: list[str] = Field(
        default_factory=list, description="触发本次调整的 diagnosis_component.id"
    )
    applied: bool = Field(
        default=True,
        description="grade=insufficient 或不可比占多数时为 False——不自动改变任何参数",
    )
    note: str = Field(description="用户可见的中文提示")
