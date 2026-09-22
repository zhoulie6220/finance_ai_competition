"""编排状态机：任务与步骤的合法状态转移。

**为什么把转移写成一张表而不是散在 if 里**：比赛要求「执行过程可追溯」。
如果状态可以被任意赋值，那么「任务已完成」这句话到底意味着什么就没人说得清——
一个已经 `succeeded` 的任务被再次置为 `running`，时间线就会前后矛盾，
而且不会有任何报错。把合法转移集中成一张表，非法转移当场抛异常。

状态取值与 `app/schemas/enums.py` 的 `TaskStatus` / `StepStatus` 逐一对应，
也与 `schema.sql` 里两张表的 CHECK 对应。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.schemas.enums import StepStatus, TaskStatus


class Clock(Protocol):
    """可注入的时钟。

    引擎层明令禁止 `datetime.now()`（时间必须由参数传入），编排层沿用同一条规矩：
    测试里换成固定时钟，整条时间线就是逐字节可复现的，不用去 mock 全局函数。
    """

    def now(self) -> str:
        """返回 ISO8601 字符串。"""
        ...


@dataclass(frozen=True)
class SystemClock:
    """真实时钟。**只有它**允许读系统时间。"""

    def now(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class FixedClock:
    """固定时钟，供测试与离线回放使用。"""

    ts: str = "2026-09-22T00:00:00+00:00"

    def now(self) -> str:
        return self.ts


class IllegalTransition(RuntimeError):
    """试图做一次状态机不允许的转移。

    刻意用异常而不是静默钳制：静默钳制会让「为什么这一步没跑」变得无从查起。
    """

    def __init__(self, kind: str, current: str, target: str, allowed: set[str]) -> None:
        self.kind, self.current, self.target, self.allowed = kind, current, target, allowed
        super().__init__(
            f"{kind} 不能从 {current!r} 转移到 {target!r}；"
            f"当前状态允许的转移到：{sorted(allowed) or '（终态，无处可去）'}"
        )


# --------------------------------------------------------------------------- 任务

#: 任务状态的合法转移。键是当前状态，值是允许到达的状态集合。
TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    # 建出来还没规划
    TaskStatus.PENDING: frozenset({TaskStatus.PLANNED, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    # 规划好了，等着开跑或等人确认
    TaskStatus.PLANNED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.WAITING_CONFIRM, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    # 跑到一半可能停下来等人（例如「不可比年度是否需要剔除」必须会计确认）
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_CONFIRM,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    # 人确认之后回到执行
    TaskStatus.WAITING_CONFIRM: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    # 以下三个是终态：没有出边。
    # 终态还能被改的话，「任务已完成」就不再是一个可依赖的事实。
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

TASK_TERMINAL: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


# --------------------------------------------------------------------------- 步骤

STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset(
        {StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.FAILED}
    ),
    # 重试是**回到 queue**而不是直接再跑：retrying 只能转回 running，
    # 中间不允许直接跳 succeeded，否则「重试过」这件事在时间线上就消失了。
    StepStatus.RUNNING: frozenset(
        {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.RETRYING, StepStatus.SKIPPED}
    ),
    StepStatus.RETRYING: frozenset({StepStatus.RUNNING, StepStatus.FAILED}),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset({StepStatus.RETRYING}),
    StepStatus.SKIPPED: frozenset(),
}

#: 步骤终态 = **没有出边**的状态。
#:
#: ⚠ `failed` 不在这里：它可以转移到 `retrying`。把它算作终态会让「重跑失败的任务」
#:   无从下手——要么绕开状态机（那状态机就形同虚设），要么让失败的任务永远修不好。
#:   注意这与**任务**终态不同：任务失败就是终局，步骤失败还可以重来。
STEP_TERMINAL: frozenset[StepStatus] = frozenset(
    {StepStatus.SUCCEEDED, StepStatus.SKIPPED}
)


def can_transition(
    table: dict[TaskStatus, frozenset[TaskStatus]]
    | dict[StepStatus, frozenset[StepStatus]],
    current,
    target,
) -> bool:
    return target in table.get(current, frozenset())


def assert_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    if current == target:
        return  # 幂等：重复写同一个状态是无害的
    if not can_transition(TASK_TRANSITIONS, current, target):
        raise IllegalTransition("任务", current.value, target.value,
                                set(TASK_TRANSITIONS[current]))


def assert_step_transition(current: StepStatus, target: StepStatus) -> None:
    if current == target:
        return
    if not can_transition(STEP_TRANSITIONS, current, target):
        raise IllegalTransition("步骤", current.value, target.value,
                                set(STEP_TRANSITIONS[current]))


def is_task_terminal(status: TaskStatus) -> bool:
    return status in TASK_TERMINAL


def is_step_terminal(status: StepStatus) -> bool:
    """这一步是否已经不会再自行推进（成功或跳过）。

    失败**不算**：失败可以重试，所以恢复一个任务时它应当被再次尝试。
    """
    return status in STEP_TERMINAL


# --------------------------------------------------------------------------- SSE 事件类型

#: SSE 事件类型白名单，与 `app/schemas/task.py::TaskEvent.type` 的说明一致。
#:
#: 做成白名单而不是自由字符串：前端按类型分发处理逻辑，多一个拼错的事件名
#: 不会报错，只会让那一帧**静默不显示**，时间线上少一格谁也不会发现。
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "plan.created",
        "plan.confirmed",
        "step.started",
        "step.succeeded",
        "step.failed",
        "step.skipped",
        "step.retrying",
        "tool.called",
        "tool.result",
        "llm.called",
        "llm.result",
        "progress",
        "warning",
        "evidence.attached",
        "review.required",
        "task.completed",
        "task.failed",
        "heartbeat",
    }
)


def assert_event_type(type_: str) -> None:
    if type_ not in EVENT_TYPES:
        raise ValueError(
            f"未知的 SSE 事件类型 {type_!r}。"
            f"允许的取值见 app/agents/state.py::EVENT_TYPES 与 app/schemas/task.py"
        )


def transition_hook(
    on_change: Callable[[TaskStatus | StepStatus, TaskStatus | StepStatus], None],
) -> Callable[[TaskStatus | StepStatus, TaskStatus | StepStatus], None]:
    """把「校验 + 通知」包成一个回调，供编排器在落库前统一过一道。

    落库与推送事件必须是**同一个动作**的两面：只推事件不落库，刷新页面时间线就空了；
    只落库不推事件，页面就停在原地。这里让两者共用一个入口。
    """

    def apply(current, target) -> None:
        if isinstance(current, TaskStatus):
            assert_task_transition(current, target)
        else:
            assert_step_transition(current, target)
        on_change(current, target)

    return apply
