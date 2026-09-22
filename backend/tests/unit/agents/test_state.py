"""编排状态机的回归测试。

状态机的价值全在**拦住非法转移**上。如果它只是「一张描述性表格」，
那么一个已经 `succeeded` 的任务还能被改成 `running`，时间线就会前后矛盾，
而且没有任何报错——这类问题在演示现场才会暴露，且当时无从查起。

所以这里的断言分两半：
  1. 合法转移必须放行（否则状态机太紧，正常流程会卡住）
  2. 非法转移必须报错（否则它等于不存在）
"""

from __future__ import annotations

import pytest

from app.agents.state import (
    EVENT_TYPES,
    STEP_TERMINAL,
    STEP_TRANSITIONS,
    TASK_TERMINAL,
    TASK_TRANSITIONS,
    FixedClock,
    IllegalTransition,
    SystemClock,
    assert_event_type,
    assert_step_transition,
    assert_task_transition,
    is_step_terminal,
    is_task_terminal,
)
from app.schemas.enums import StepStatus, TaskStatus


# ---------------------------------------------------------------- 任务状态

LEGAL_TASK_MOVES = [
    (TaskStatus.PENDING, TaskStatus.PLANNED),
    (TaskStatus.PENDING, TaskStatus.CANCELLED),
    (TaskStatus.PLANNED, TaskStatus.RUNNING),
    (TaskStatus.PLANNED, TaskStatus.WAITING_CONFIRM),
    (TaskStatus.RUNNING, TaskStatus.SUCCEEDED),
    (TaskStatus.RUNNING, TaskStatus.WAITING_CONFIRM),
    (TaskStatus.RUNNING, TaskStatus.FAILED),
    (TaskStatus.WAITING_CONFIRM, TaskStatus.RUNNING),
]


@pytest.mark.parametrize("current,target", LEGAL_TASK_MOVES)
def test_legal_task_transitions_are_allowed(current, target) -> None:
    assert_task_transition(current, target)  # 不抛异常即通过


@pytest.mark.parametrize(
    "current,target",
    [
        # 终态必须真的终态：改得动的话，「任务已完成」就不是一个可依赖的事实
        (TaskStatus.SUCCEEDED, TaskStatus.RUNNING),
        (TaskStatus.SUCCEEDED, TaskStatus.FAILED),
        (TaskStatus.FAILED, TaskStatus.RUNNING),
        (TaskStatus.CANCELLED, TaskStatus.RUNNING),
        # 不能跳过规划直接开跑：跳过之后没人说得清它当初打算干什么
        (TaskStatus.PENDING, TaskStatus.RUNNING),
        # 不能跳过执行直接成功
        (TaskStatus.PLANNED, TaskStatus.SUCCEEDED),
        # 待确认状态不能直接判成功——那等于把「等人确认」这步吞掉了
        (TaskStatus.WAITING_CONFIRM, TaskStatus.SUCCEEDED),
    ],
)
def test_illegal_task_transitions_raise(current, target) -> None:
    with pytest.raises(IllegalTransition):
        assert_task_transition(current, target)


def test_repeating_the_same_status_is_a_noop() -> None:
    """重复写同一个状态不算非法转移。

    编排器在重试、幂等重放等路径上会重复设置状态；把它当异常会让这些路径
    不得不加一堆 `if status != x`，而那些判断迟早会漏一处。
    """
    for status in TaskStatus:
        assert_task_transition(status, status)


def test_error_message_names_both_states_and_the_options() -> None:
    """报错要能直接定位问题，而不是只说「转移非法」。"""
    with pytest.raises(IllegalTransition) as exc:
        assert_task_transition(TaskStatus.SUCCEEDED, TaskStatus.RUNNING)
    msg = str(exc.value)
    assert "succeeded" in msg and "running" in msg
    # 终态没有出边，报错里要说明这一点，否则读者会以为表格漏了
    assert "终态" in msg


def test_terminal_sets_match_the_table() -> None:
    """`*_TERMINAL` 常量必须与转移表一致（都是没有出边的状态）。"""
    assert {s for s in TaskStatus if not TASK_TRANSITIONS[s]} == set(TASK_TERMINAL)
    assert {s for s in StepStatus if not STEP_TRANSITIONS[s]} == set(STEP_TERMINAL)


def test_every_status_appears_in_the_table() -> None:
    """枚举里新增一个状态却忘了加进转移表，那个状态就会永远卡住。

    `TaskStatus` 与 `schema.sql` 的 CHECK 是对应的，改一处不改另一处
    会得到「数据库放行、状态机不认识」的状态。
    """
    assert set(TASK_TRANSITIONS) == set(TaskStatus)
    assert set(STEP_TRANSITIONS) == set(StepStatus)


# ---------------------------------------------------------------- 步骤状态


def test_step_retry_must_pass_through_retrying() -> None:
    """重试必须经过 `retrying` 中转。

    直接从 failed 跳回 running 的话，「这一步重试过」在时间线上就消失了，
    而重试次数恰好是评审判断系统稳不稳的一个凭据。
    """
    with pytest.raises(IllegalTransition):
        assert_step_transition(StepStatus.FAILED, StepStatus.RUNNING)
    assert_step_transition(StepStatus.FAILED, StepStatus.RETRYING)
    assert_step_transition(StepStatus.RETRYING, StepStatus.RUNNING)


# ---------------------------------------------------------------- SSE 事件类型


def test_known_event_types_pass() -> None:
    for t in EVENT_TYPES:
        assert_event_type(t)


def test_unknown_event_type_is_rejected() -> None:
    """事件类型做成白名单，是因为拼错不会报错、只会让那一帧静默不显示。

    时间线上少一格，看起来和「这一步本来就没有」一模一样。
    """
    with pytest.raises(ValueError, match="未知的 SSE 事件类型"):
        assert_event_type("step.suceeded")  # 少了一个 c


def test_event_types_cover_every_step_and_task_ending() -> None:
    """每个步骤状态都要有对应的事件，否则该状态的变化前端收不到。"""
    for t in ("step.started", "step.succeeded", "step.failed", "step.skipped",
              "step.retrying"):
        assert t in EVENT_TYPES
    for t in ("task.completed", "task.failed"):
        assert t in EVENT_TYPES


# ---------------------------------------------------------------- 时钟


def test_fixed_clock_is_exactly_reproducible() -> None:
    assert FixedClock().now() == FixedClock().now()
    assert FixedClock("2020-01-01").now() == "2020-01-01"


def test_system_clock_is_iso8601() -> None:
    from datetime import datetime

    parsed = datetime.fromisoformat(SystemClock().now())
    assert parsed.tzinfo is not None, "必须是带时区的时间，否则跨机器比较会出错"


def test_orchestrator_only_uses_the_injected_clock() -> None:
    """编排层不得直接读系统时间。

    与引擎「时间由参数传入」是同一条规矩：测试里注入固定时钟，
    整条时间线就是逐字节可复现的，不用去 mock 全局函数。
    """
    import inspect

    from app.agents import orchestrator

    src = inspect.getsource(orchestrator)
    assert "datetime.now" not in src, "编排器里出现了直接读系统时间的代码"
    assert "time.time" not in src, "编排器里出现了直接读系统时间的代码"
