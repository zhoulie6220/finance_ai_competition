"""诊断指数 → 估值情景参数的传导。

纯函数，零 IO。口径见会计口径 v1.1 §A.7。

## 这个模块存在的全部意义：**指数不许黑箱式改目标价**

赛事把「计算可复算、过程可追溯」写成硬要求。一个会自己改目标价的指数
恰恰是它的反面——用户看到一个数字变了，却不知道是谁改的、凭什么改。

所以这里只做一件事：**把分数映射成一组情景权重与参数建议**，
且**默认仅提示、由人工确认后重算**。

## 刻意不做的事

* **不产出目标价。** `ScenarioAdjustment` 里**没有价格字段**——
  这不是靠自觉，是数据类型层面的保证（下面的测试会断言这一点）。
  估值输出永远是区间，单点目标价会隐藏不确定性。
* **不自动改 WACC 与永续增长率。** v1.1 明确：这两个需要独立依据，
  不得仅因叙事指数低而修改。旧稿规定指数自动上调 WACC +0.5/+1.0 个百分点，
  已按 v1.1 移除。
* **不重复惩罚。** 同一风险不能同时压低收入、利润率并抬高 WACC。

## 传导只针对有直接证据的经营参数

    销量     → 收入
    单位成本 → 毛利及 EBIT
    回款天数 → 应收与营运资本

指数不直接说「收入该是多少」，它说的是「有一组证据指向增长假设需要复核」，
具体取值由人工确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Grade = Literal["high", "medium", "low", "insufficient_evidence"]

#: 情景顺序固定为 基准 / 乐观 / 压力。**顺序不能改**——
#: 页面按这个顺序渲染，改了会让权重与标签错位且不报错。
SCENARIO_ORDER = ("base", "optimistic", "stress")
SCENARIO_LABELS = {"base": "基准", "optimistic": "乐观", "stress": "压力"}


@dataclass(frozen=True)
class ScenarioWeights:
    """三个情景的权重。三者之和必须为 1。"""

    base: Decimal
    optimistic: Decimal
    stress: Decimal

    def __post_init__(self) -> None:
        total = self.base + self.optimistic + self.stress
        # 容差取 1e-9：rule_config 里的小数是手写的，0.6+0.25+0.15 在
        # Decimal 下也未必恰好是 1.000000
        if abs(total - Decimal(1)) > Decimal("0.000000001"):
            raise ValueError(
                f"情景权重之和必须为 1，当前为 {total}。"
                f"和不为 1 的话页面上的权重加起来不是 100%，"
                f"而每个单独看都像是正常数字。"
            )
        for name, value in (
            ("base", self.base), ("optimistic", self.optimistic), ("stress", self.stress)
        ):
            if not (Decimal(0) <= value <= Decimal(1)):
                raise ValueError(f"{name} 权重 {value} 不在 [0,1] 之间")

    def as_dict(self) -> dict[str, str]:
        """出网形状。**字符串**，不经过 float。"""
        return {
            "base": str(self.base),
            "optimistic": str(self.optimistic),
            "stress": str(self.stress),
        }

    def describe(self) -> str:
        return (
            f"基准 {self.base} / 乐观 {self.optimistic} / 压力 {self.stress}"
        )


@dataclass(frozen=True)
class ScenarioAdjustment:
    """一次传导的结论。

    ⚠ **没有价格字段，也不该有。** 想加「目标价」的话，那不是加一个字段的事，
    而是把整个「估值永远是区间」的设计改掉了——先看 docs/03 §五。
    """

    grade: Grade
    weights: ScenarioWeights | None
    #: 建议沿用的历史统计口径（如 historical_median），不是具体数值
    revenue_growth_ref: str | None
    #: **是否必须经人工确认后才重算**。v1.1 下这个几乎总是 True。
    requires_human_confirmation: bool
    #: 工作台应该做什么
    action: str
    #: 估值应该做什么
    valuation_action: str
    #: 给用户看的一句话
    user_hint: str
    notes: tuple[str, ...] = ()

    @property
    def changes_valuation(self) -> bool:
        """这次传导会不会改变估值参数。

        `insufficient_evidence` 时**不发生由指数驱动的估值调整**——
        证据不足就不该影响估值，哪怕只是权重。
        """
        return self.grade != "insufficient_evidence" and self.weights is not None


#: 四个等级对应的动作。文案与 v1.1 §A.7 的表格逐条对应。
_ACTIONS: dict[Grade, tuple[str, str, str]] = {
    "high": (
        "展示支持证据及仍存在的风险",
        "保留原有情景；**不得自动提高增长率**",
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
    "insufficient_evidence": (
        "展示缺口与补资料入口",
        "**不发生由指数驱动的估值调整**",
        "未传导至估值，仅提示人工复核",
    ),
}

#: 默认权重。初值与 rule_config 的 mapping.*.weights 一致。
#: **注：这是研究者设定的主观假设，不是模型估计的概率**，页面须如此标注。
DEFAULT_WEIGHTS: dict[Grade, ScenarioWeights] = {
    "high": ScenarioWeights(Decimal("0.60"), Decimal("0.25"), Decimal("0.15")),
    "medium": ScenarioWeights(Decimal("0.50"), Decimal("0.20"), Decimal("0.30")),
    "low": ScenarioWeights(Decimal("0.35"), Decimal("0.15"), Decimal("0.50")),
    "insufficient_evidence": ScenarioWeights(
        Decimal("0.50"), Decimal("0.20"), Decimal("0.30")
    ),
}

#: 建议沿用的历史口径。只是**指向一个统计量**，不是具体数值。
_REVENUE_REFS: dict[Grade, str | None] = {
    "high": "historical_median",
    "medium": "historical_p25",
    "low": "historical_p25_x0.9",
    "insufficient_evidence": None,
}

NOTE_SUBJECTIVE = "情景权重是研究者设定的主观假设，不是模型估计的概率。"
NOTE_NO_WACC = (
    "WACC 与永续增长率需独立依据，**不得仅因叙事指数低而修改**——"
    "本次传导不涉及这两个参数。"
)
NOTE_NO_DOUBLE_PENALTY = (
    "同一风险不得重复惩罚：不能同时压低收入、利润率并抬高 WACC。"
)


def map_index_to_scenarios(
    grade: Grade,
    *,
    weights_by_grade: dict[Grade, ScenarioWeights] | None = None,
) -> ScenarioAdjustment:
    """把指数分级映射成情景调整建议。

    `weights_by_grade` 由调用方从 rule_config 的 `mapping.*.weights` 读出来传入；
    不给则用代码里的默认值。**两者必须一致**，否则页面显示的口径
    与实际生效的会对不上——所以 skill 层应当总是从库里读。
    """
    weights = (weights_by_grade or DEFAULT_WEIGHTS).get(grade)
    action, valuation_action, user_hint = _ACTIONS[grade]

    notes = [NOTE_SUBJECTIVE, NOTE_NO_WACC, NOTE_NO_DOUBLE_PENALTY]
    if grade == "insufficient_evidence":
        notes.append(
            "证据不足时**不做任何由指数驱动的估值调整**——"
            "包括情景权重。给一组「差不多的权重」会让闸门形同虚设。"
        )
        # 不足以出分时不提供权重：给了就会被用上
        weights = None

    return ScenarioAdjustment(
        grade=grade,
        weights=weights,
        revenue_growth_ref=_REVENUE_REFS[grade],
        # v1.1 下几乎总是要人工确认；只有「不足以出分」是「根本不改」
        requires_human_confirmation=grade != "insufficient_evidence",
        action=action,
        valuation_action=valuation_action,
        user_hint=user_hint,
        notes=tuple(notes),
    )


def parse_weights(raw: str | list[str]) -> ScenarioWeights:
    """解析 rule_config 里的 `[0.60,0.25,0.15]`。

    顺序是 基准 / 乐观 / 压力。解析失败**抛错而不是给默认值**——
    静默回退到默认权重会让「参数被改坏了」表现为「参数没生效」。
    """
    if isinstance(raw, str):
        text = raw.strip().lstrip("[").rstrip("]")
        parts = [p.strip() for p in text.split(",") if p.strip()]
    else:
        parts = [str(p) for p in raw]

    if len(parts) != len(SCENARIO_ORDER):
        raise ValueError(
            f"情景权重需要 {len(SCENARIO_ORDER)} 个数（基准/乐观/压力），"
            f"收到 {len(parts)} 个：{raw!r}"
        )
    base, optimistic, stress = (Decimal(p) for p in parts)
    return ScenarioWeights(base=base, optimistic=optimistic, stress=stress)


__all__ = [
    "DEFAULT_WEIGHTS",
    "NOTE_NO_DOUBLE_PENALTY",
    "NOTE_NO_WACC",
    "NOTE_SUBJECTIVE",
    "SCENARIO_LABELS",
    "SCENARIO_ORDER",
    "ScenarioAdjustment",
    "ScenarioWeights",
    "map_index_to_scenarios",
    "parse_weights",
]
