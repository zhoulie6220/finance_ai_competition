"""Skill 契约。

Skill 与 Tool 的区别是**粒度**：Tool 是一次可以独立测试的调用（算一个比率、
查一页原文），Skill 是一条有顺序的流程（解析年报 → 落库 → 校验 → 一致性诊断）。
编排器只认 Skill，Skill 只认 Tool —— 中间不再有第三层概念。

赛事要求「智能体编排框架」与「Skill」分列两个模块，这里的划分是：

    orchestrator  建任务、排步骤、发事件、落库、守状态机      ← 不关心业务
    Skill         把一个意图拆成若干步骤，逐步调 Tool          ← 业务在这里
    Tool          一次确定的调用，输入输出都有 JSON Schema     ← 可单测、可暴露给 MCP
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.tools.registry import ToolOutcome


@dataclass(frozen=True)
class SkillRequest:
    """一次请求的输入。"""

    user_input: str
    project_id: str | None = None
    #: 用户在前端显式指定的额外参数（如指定年份、指定可比公司）
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedStep:
    """计划中的一步。

    `depends_on` 用**本计划内的下标**而不是 step_id：规划发生在写库之前，
    那时还没有 step_id。编排器落库时再把它翻译成真实 id。
    """

    name: str
    #: 要调的工具名。为 None 表示这一步由 Skill 自己处理（如汇总、组织文字）。
    tool_name: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[int, ...] = ()
    #: 这一步是否必须人工确认后才能继续。置 True 时任务进入 waiting_confirm。
    #: 会计口径的判定（不可比年度是否剔除、减值是否加回）一律走这条路。
    requires_review: bool = False


@dataclass(frozen=True)
class StepOutcome:
    """一步的执行结果。"""

    summary: str
    value: Any = None
    requires_review: bool = False
    #: 需要人工确认时给用户看的问题
    review_prompt: str | None = None
    evidence_refs: list[str] = field(default_factory=list)


def tool_result(outcome: StepOutcome | None) -> dict[str, Any]:
    """从上一步的产出里取出**工具返回的那个 dict**。

    ⚠ 编排器会把工具结果包一层再交给后续步骤：

        outcome.value = {"result": <工具返回值>, "formula": ..., "inputs": ...}

    包这一层是为了「点击结论回到计算过程」——公式与入参必须跟着值一起走。
    但**包装对被包装者是隐形的**：Skill 想读 `value["passed"]` 时会拿到 `None`，
    而 `None` 在 `if x is False` 这种判断里等于「没问题」。

    这个坑真实发生过：自检 Skill 的汇总读 `value.get("passed")`，
    字典检查报了 1 个问题，汇总照样输出「自检通过」——检查跑了、发现了、
    然后被静默吞掉。所以取原始值一律走这里，不要自己记住要下钻一层：
    忘了下钻**不会报错**，只会让所有判断都走「一切正常」那条分支。
    """
    if outcome is None or not isinstance(outcome.value, dict):
        return {}
    inner = outcome.value.get("result")
    return inner if isinstance(inner, dict) else {}


class Skill(Protocol):
    """一个 Skill。"""

    key: str
    name_cn: str
    description_cn: str

    def can_handle(self, request: SkillRequest) -> bool:
        """这个 Skill 能不能处理该请求。

        路由先用规则（关键词、显式指定 /skill）判，判不出来再交给 LLM 兜底。
        **顺序很重要**：反过来会让简单请求也走一次模型调用，既慢又不可复现。
        """
        ...

    def plan(self, request: SkillRequest) -> Sequence[PlannedStep]:
        """把一个意图拆成有序步骤。"""
        ...

    async def run(
        self, step: PlannedStep, ctx: Any, prior: dict[int, "StepOutcome"]
    ) -> StepOutcome:
        """执行一步。

        `ctx` 是 `app.agents.orchestrator.StepContext`。
        `prior` 是**本次运行**中已成功步骤的结果，按计划下标索引 —— 汇总类步骤
        靠它拿到前面各步算出了什么。

        刻意不把 `prior` 存进 StepContext：它是「这一步之前发生了什么」，
        每跑一步都要换一份，而 ctx 在一次运行里是稳定的。
        两者混在一起，迟早会有人读到上一个任务的残留结果。
        """
        ...


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> Skill:
        if skill.key in self._skills:
            raise ValueError(f"Skill {skill.key!r} 重复注册")
        self._skills[skill.key] = skill
        return skill

    def get(self, key: str) -> Skill:
        try:
            return self._skills[key]
        except KeyError:
            raise KeyError(
                f"未登记的 Skill {key!r}。已登记：{sorted(self._skills)}"
            ) from None

    def all(self) -> list[Skill]:
        return [self._skills[k] for k in sorted(self._skills)]

    def route(self, request: SkillRequest) -> Skill | None:
        """规则路由。返回 None 表示规则判不出来，调用方可以交给 LLM 兜底。"""
        for skill in self.all():
            if skill.can_handle(request):
                return skill
        return None


REGISTRY = SkillRegistry()


def as_step_outcome(outcome: ToolOutcome, *, requires_review: bool = False) -> StepOutcome:
    """把 ToolOutcome 适配成 StepOutcome。

    两个类型的字段大部分重合，但**不合并**：ToolOutcome 是工具的作者写出来的，
    StepOutcome 是编排器要的。合并之后工具作者就得理解「人工确认」这套编排概念，
    而那些概念和「算一个比率」毫无关系。
    """
    return StepOutcome(
        summary=outcome.summary,
        value=outcome.value,
        evidence_refs=list(outcome.evidence_refs),
        requires_review=requires_review,
    )
