"""叙事—财务一致性诊断指数。

纯函数，零 IO。口径见 `docs/04-index-rules.md`（会计口径 v1.1）。

    I_raw = 50 + 20·H + 20·C + 5·R − 10·P − 15·Q
    I     = min(100, max(0, I_raw))

| 分项 | 含义 | 取值 |
|---|---|---|
| H | 历史主张 s 的等权平均 | **[−1, 1]** |
| C | 当期主张 s 的等权平均 | **[−1, 1]** |
| R | 风险披露充分度 | [0, 1] |
| P | 缺乏可验证性的实质表述占比 | [0, 1] |
| Q | 已确认触发的质量检查占比 | [0, 1] |

⚠ **H、C 是 [−1,1] 不是 [0,1]，这是 v1.1 最关键的一处修订。**
旧定义下五个分项都是 0–1，中性证据只能取 0.5，于是 H=C=0.5 会算出
50 + 20×0.5 + 20×0.5 = **70 分**——正好压在「一致性较高」的分界线上，
**全中性的公司反而得分最高**。改成 [−1,1] 之后全中性得 50 分。

下面 `_require_s_values` 会拒绝任何不在 {−1, 0, +1} 里的 s 值——
喂进来 0.5 说明调用方还在用旧口径，那会让分数整体偏高 10 分，
而且**不报任何错**。

## 闸门

四条件全满足才出分，否则 `insufficient_evidence` 且 `score=None`：

    1. 覆盖率 n/N ≥ 0.60
    2. 有效观测 n ≥ 5
    3. **H 与 C 各至少一条观测**
    4. **R、P、Q 均可算且核验完成**

第 3 条拦的是「全是本期主张」——历史那一项没有观测，
拿 0 去填等于把「没评估」当成「无异议」。
第 4 条拦的是「适用项没核完就当没触发」。

**闸门不过时绝不用 0 分或 50 分代替。** 页面上出现一个大号数字，
人工复核的入口就没人看了。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Status = Literal["scored", "insufficient_evidence"]
Grade = Literal["high", "medium", "low", "insufficient_evidence"]

SCORED: Status = "scored"
INSUFFICIENT: Status = "insufficient_evidence"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"   # 与 grade 同名，见 v1.1 §A.4

PLACES = Decimal("0.000001")

# 结论边界模板。**模板生成而不是 LLM 生成**——这段话是给人看的免责，
# 让模型自由发挥会把「存在待核查风险」写成「管理层叙事虚假」。
CONCLUSION_BOUNDARY = (
    "本结果为基于公开资料的研究辅助与风险提示，不构成审计意见或证券买卖建议。"
    "指数是规则型研究工具，不代表管理层诚信、欺诈概率或股票收益，"
    "也不构成虚假披露的判断。"
)


@dataclass(frozen=True)
class IndexConfig:
    """权重与阈值。初值与 rule_config 的 index.* 一致。"""

    base_score: Decimal = Decimal("50")
    weight_history: Decimal = Decimal("20")
    weight_current: Decimal = Decimal("20")
    weight_risk_shift: Decimal = Decimal("5")
    penalty_template: Decimal = Decimal("10")
    penalty_quality_conflict: Decimal = Decimal("15")
    grade_high_min: Decimal = Decimal("70")
    grade_low_max: Decimal = Decimal("45")
    min_coverage: Decimal = Decimal("0.60")
    min_observations: int = 5

    def __post_init__(self) -> None:
        if not (Decimal(0) <= self.min_coverage <= Decimal(1)):
            raise ValueError(f"min_coverage 是比例，收到 {self.min_coverage}")
        if self.min_observations < 1:
            raise ValueError("min_observations 至少为 1")
        if self.grade_low_max >= self.grade_high_min:
            raise ValueError(
                "低级别上限必须小于高级别下限，否则中间那一档不存在"
            )


@dataclass(frozen=True)
class RatioComponent:
    """一个比例型分项（R / P / Q）。"""

    name: str
    label_cn: str
    numerator: int | None
    denominator: int | None
    verified: bool
    note: str = ""

    @property
    def computable(self) -> bool:
        """可算且**核验完成**。

        ⚠ 「核验完成」与「分母不为零」是两件事。分母为 0 只说明没有适用项，
        而「适用项没核完」在数值上和它长得一样——都是算不出来。
        所以必须由调用方显式给出 `verified`，不能靠分母猜。
        """
        return (
            self.verified
            and self.denominator is not None
            and self.denominator > 0
            and self.numerator is not None
        )

    @property
    def value(self) -> Decimal | None:
        if not self.computable:
            return None
        assert self.numerator is not None and self.denominator
        return (Decimal(self.numerator) / Decimal(self.denominator)).quantize(PLACES)

    def describe(self) -> str:
        if self.computable:
            return f"{self.numerator}/{self.denominator} = {self.value}"
        if not self.verified:
            return f"未核验完成（{self.note or '适用项尚未核完'}）"
        return f"无适用项（分母为 0{('；' + self.note) if self.note else ''}）"


@dataclass(frozen=True)
class IndexInput:
    """指数的全部输入。"""

    #: H：历史主张的 s 值。每个必须是 −1 / 0 / +1。
    history_scores: tuple[Decimal, ...]
    #: C：当期主张的 s 值。
    current_scores: tuple[Decimal, ...]
    #: n：拿到 supported / neutral / contradicted 的条数。
    observation_count: int
    #: N：已到验证期、对象与目标可识别的去重主张数。
    denominator_count: int
    risk: RatioComponent
    template: RatioComponent
    quality: RatioComponent


@dataclass(frozen=True)
class IndexResult:
    status: Status
    grade: Grade
    score: Decimal | None
    history: Decimal | None
    current: Decimal | None
    risk: Decimal | None
    template: Decimal | None
    quality: Decimal | None
    coverage: Decimal | None
    history_count: int
    current_count: int
    observation_count: int
    denominator_count: int
    insufficient_reason: str | None
    formula: str
    conclusion_boundary: str = CONCLUSION_BOUNDARY
    method_version: str = "index:v1.1"

    @property
    def ok(self) -> bool:
        """是否真的出了分。下游一律先检查这个。"""
        return self.status == SCORED

    def coverage_line(self) -> str:
        if self.denominator_count == 0:
            return "覆盖率为 0/0，没有可判定的主张"
        return (
            f"覆盖率 {self.observation_count}/{self.denominator_count}"
            f" = {self.coverage}"
        )


# ---------------------------------------------------------------- 主入口


def compute_index(data: IndexInput, cfg: IndexConfig | None = None) -> IndexResult:
    """算指数。闸门不过时不产出分数。"""
    cfg = cfg or IndexConfig()
    _require_s_values(data.history_scores, "H")
    _require_s_values(data.current_scores, "C")

    history = _mean(data.history_scores)
    current = _mean(data.current_scores)

    coverage = None
    if data.denominator_count > 0:
        coverage = (
            Decimal(data.observation_count) / Decimal(data.denominator_count)
        ).quantize(PLACES)

    # ---- 闸门四条件，逐条检查并给出**具体**原因 --------------------------
    failures: list[str] = []
    if coverage is None:
        failures.append("没有可判定的主张（分母为 0）")
    elif coverage < cfg.min_coverage:
        failures.append(
            f"覆盖率 {coverage} 低于要求的 {cfg.min_coverage}"
            f"（{data.observation_count}/{data.denominator_count}）"
        )
    if data.observation_count < cfg.min_observations:
        failures.append(
            f"有效观测 {data.observation_count} 条，少于要求的 "
            f"{cfg.min_observations} 条"
        )
    if not data.history_scores:
        failures.append("H（历史兑现度）没有观测——全是本期主张时不输出完整指数")
    if not data.current_scores:
        failures.append("C（当期一致性）没有观测")
    for component in (data.risk, data.template, data.quality):
        if not component.computable:
            failures.append(f"{component.label_cn}不可算或未核验完成：{component.describe()}")

    if failures:
        return _fail("；".join(failures), cfg, data, coverage)

    assert history is not None and current is not None
    assert data.risk.value is not None
    assert data.template.value is not None
    assert data.quality.value is not None

    raw = (
        cfg.base_score
        + cfg.weight_history * history
        + cfg.weight_current * current
        + cfg.weight_risk_shift * data.risk.value
        - cfg.penalty_template * data.template.value
        - cfg.penalty_quality_conflict * data.quality.value
    )
    score = min(Decimal(100), max(Decimal(0), raw)).quantize(PLACES)

    return IndexResult(
        status=SCORED,
        grade=_grade(score, cfg),
        score=score,
        history=history,
        current=current,
        risk=data.risk.value,
        template=data.template.value,
        quality=data.quality.value,
        coverage=coverage,
        history_count=len(data.history_scores),
        current_count=len(data.current_scores),
        observation_count=data.observation_count,
        denominator_count=data.denominator_count,
        insufficient_reason=None,
        formula=_formula(history, current, data, cfg, raw),
    )


def _fail(
    reason: str,
    cfg: IndexConfig,
    data: IndexInput,
    coverage: Decimal | None,
) -> IndexResult:
    """构造失败结果。**刻意不产出任何分数**。

    如果失败也返回一个「带警告的数字」，页面一定会把它渲染成一个大号分数，
    人工复核入口就形同虚设——这是 v1.1 与 docs/03 都反复强调的一点。
    """
    return IndexResult(
        status=INSUFFICIENT,
        grade=INSUFFICIENT_EVIDENCE,
        score=None,
        history=_mean(data.history_scores),
        current=_mean(data.current_scores),
        risk=data.risk.value,
        template=data.template.value,
        quality=data.quality.value,
        coverage=coverage,
        history_count=len(data.history_scores),
        current_count=len(data.current_scores),
        observation_count=data.observation_count,
        denominator_count=data.denominator_count,
        insufficient_reason=reason,
        formula="（未产出总分）",
    )


# ---------------------------------------------------------------- 内部


def _require_s_values(scores: tuple[Decimal, ...], label: str) -> None:
    """★ s 值只能是 −1 / 0 / +1。

    喂进来 0.5 说明调用方还在用旧口径（中性编码成 0.5）。那会让 H 或 C
    整体偏高，最终分数**虚高 10 分左右**，而且不报任何错——
    全中性的公司会拿到 70 分而不是 50 分，正好跨过「一致性较高」的分界线。
    """
    allowed = {Decimal(-1), Decimal(0), Decimal(1)}
    bad = sorted({s for s in scores if s not in allowed})
    if bad:
        raise ValueError(
            f"{label} 的 s 值只能是 −1 / 0 / +1，收到 {bad}。"
            f"若出现 0.5，说明还在用旧口径（中性=0.5）——那会让分数虚高、"
            f"全中性的公司反而得分最高，而且不报错。会计口径 v1.1 §A.1 "
            f"已把 H、C 改成 [−1, 1]。"
        )


def _mean(scores: tuple[Decimal, ...]) -> Decimal | None:
    """等权平均。**无观测时返回 None，不返回 0。**

    返回 0 会把「没有观测」伪装成「全中性」——而全中性的贡献是 0 分，
    两者在公式里数值相同、含义完全相反。
    """
    if not scores:
        return None
    return (sum(scores, Decimal(0)) / Decimal(len(scores))).quantize(PLACES)


def _grade(score: Decimal, cfg: IndexConfig) -> Grade:
    """按**未四舍五入**的值分级。

    先四舍五入再比会出现 69.9996 显示成 70.00 却判 medium 的错位——
    页面显示 70.00、文案说「支持与风险并存」，看的人只会觉得系统坏了。
    """
    if score >= cfg.grade_high_min:
        return "high"
    if score < cfg.grade_low_max:
        return "low"
    return "medium"


def _formula(
    history: Decimal,
    current: Decimal,
    data: IndexInput,
    cfg: IndexConfig,
    raw: Decimal,
) -> str:
    return (
        f"I = {cfg.base_score} + {cfg.weight_history}×H({history}) "
        f"+ {cfg.weight_current}×C({current}) "
        f"+ {cfg.weight_risk_shift}×R({data.risk.value}) "
        f"− {cfg.penalty_template}×P({data.template.value}) "
        f"− {cfg.penalty_quality_conflict}×Q({data.quality.value}) "
        f"= {raw.quantize(PLACES)}，截断到 [0,100]"
    )


# ---------------------------------------------------------------- 场景映射


GRADE_ACTIONS: dict[Grade, tuple[str, str, str]] = {
    "high": (
        "保留原有情景",
        "**不得自动提高增长率**",
        "一致性较高，保持基准",
    ),
    "medium": (
        "展示争议项与经营假设敏感性",
        "**请求确认相关假设，不自动改值**",
        "证据部分冲突，请确认增长假设",
    ),
    "low": (
        "优先展开压力情景与反证条件",
        "**人工确认具体经营参数后重算**",
        "扩大估值区间",
    ),
    INSUFFICIENT_EVIDENCE: (
        "展示缺口与补资料入口",
        "**不发生由指数驱动的估值调整**",
        "未传导至估值，仅提示人工复核",
    ),
}
