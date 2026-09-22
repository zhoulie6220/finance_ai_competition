"""编排器与事件总线的回归测试。

这一层最怕的不是崩溃，而是**看起来跑完了**：任务显示成功、时间线却少了几格，
或者工具明明被调了、`tool_call` 表里却没有记录。对比赛来说，那等于失去了
「执行过程可追溯」这条主张的证据。

所以断言集中在「事件与数据库是否对得上」：每一条 SSE 事件，库里都有对应的行。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.agents.orchestrator import Orchestrator, OrchestratorError
from app.agents.state import (
    EVENT_TYPES,
    FixedClock,
    IllegalTransition,
    assert_task_transition,
)
from app.api.events import EventBus, format_sse
from app.db.repositories.task_repo import TaskRepository
from app.db.session import connect_memory, init_schema, load_seeds
from app.schemas.enums import StepStatus, TaskStatus

import app.skills  # noqa: F401 —— 导入即注册
from app.skills.base import REGISTRY as SKILLS


@pytest.fixture()
def con() -> sqlite3.Connection:
    c = connect_memory()
    init_schema(c)
    load_seeds(c)
    yield c
    c.close()


@pytest.fixture()
def staged(con):
    clock = FixedClock()
    repo = TaskRepository(con)
    bus = EventBus(clock=clock)
    orch = Orchestrator(repo=repo, bus=bus, clock=clock, skills=SKILLS)
    return orch, repo, bus, clock


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 建任务与规划


def test_create_task_starts_pending(staged) -> None:
    orch, repo, _, _ = staged
    task = orch.create_task("跑一次系统自检")
    assert task.status is TaskStatus.PENDING
    assert repo.get_task(task.task_id).user_input == "跑一次系统自检"


def test_empty_input_is_rejected(staged) -> None:
    orch, _, _, _ = staged
    with pytest.raises(OrchestratorError):
        orch.create_task("   ")


def test_plan_creates_steps_and_publishes_event(staged) -> None:
    orch, repo, bus, _ = staged
    task = orch.plan_task(orch.create_task("系统自检"))
    assert task.status is TaskStatus.PLANNED
    assert len(task.steps) == 4
    assert [e.type for e in bus.history(task.task_id)] == ["plan.created"]
    # 计划必须落库：只用内存对象的话，页面刷新后时间线就空了
    assert task.plan and len(task.plan["steps"]) == 4


def test_unroutable_input_fails_loudly(staged) -> None:
    """规则路由判不出来时必须失败，**不能**随便挑一个 Skill 跑。

    挑错了 Skill 的后果是跑完一遍、出一份和问题无关的结果，
    而用户以为那就是答案。
    """
    orch, repo, bus, _ = staged
    task = orch.create_task("帮我预测一下明年的钢价")
    with pytest.raises(OrchestratorError, match="无法为请求选择 Skill"):
        orch.plan_task(task)
    assert repo.get_task(task.task_id).status is TaskStatus.FAILED
    assert "task.failed" in [e.type for e in bus.history(task.task_id)]


def test_cannot_run_before_plan_confirmed(staged, con) -> None:
    """含人工复核步骤的计划必须确认后才能执行。"""
    orch, repo, _, _ = staged
    task = orch.plan_task(orch.create_task("系统自检"))
    repo.con.execute("UPDATE task SET plan_confirmed=0 WHERE task_id=?", (task.task_id,))
    task = repo.get_task(task.task_id)
    with pytest.raises(OrchestratorError, match="尚未确认"):
        run(orch.run_task(task))


# ---------------------------------------------------------------- 执行


def test_full_run_succeeds_and_records_everything(staged) -> None:
    orch, repo, bus, _ = staged
    task = orch.plan_task(orch.create_task("系统自检"))
    task = run(orch.run_task(task))

    assert task.status is TaskStatus.SUCCEEDED
    assert all(s.status is StepStatus.SUCCEEDED for s in task.steps)

    # 「工具调用情况完整记录」：3 个工具各留一条
    calls = repo.list_tool_calls(task.task_id)
    assert len(calls) == 3
    assert {c.tool_name for c in calls} == {
        "system.db_stats", "system.dictionary_check", "system.rule_config_check"
    }
    assert all(c.status == "succeeded" for c in calls)
    # 结果哈希：复现验证靠它比对
    assert all(c.result_hash for c in calls)
    # args 必须是**对象**：库里存的是 JSON 文本，仓储层不转的话接口会返回字符串，
    # 前端写 `call.args['x']` 拿到 undefined 而不报错。
    assert all(isinstance(c.args, dict) for c in calls)


def test_every_published_event_has_a_valid_type(staged) -> None:
    """发出去的事件类型必须在白名单里。

    拼错一个类型不会报错，只会让前端静默不显示那一帧。
    """
    orch, _, bus, _ = staged
    task = orch.plan_task(orch.create_task("系统自检"))
    task = run(orch.run_task(task))
    types = [e.type for e in bus.history(task.task_id)]
    assert types, "任务跑完了却没有任何事件"
    assert set(types) <= EVENT_TYPES, f"出现了未登记的事件类型：{set(types) - EVENT_TYPES}"


def test_event_sequence_is_monotonic_and_gapless(staged) -> None:
    """seq 必须从 1 开始、连续递增。

    前端靠 seq 判断时间线有没有缺格；不连续的话它无从知道是自己漏收了
    还是服务端本来就没发。
    """
    orch, _, bus, _ = staged
    task = orch.plan_task(orch.create_task("系统自检"))
    run(orch.run_task(task))
    seqs = [e.seq for e in bus.history(task.task_id)]
    assert seqs == list(range(1, len(seqs) + 1))


def test_tool_call_brackets_every_tool_execution(staged) -> None:
    """每次工具执行都要有 called 和 result 两条事件，一前一后。"""
    orch, _, bus, _ = staged
    task = orch.plan_task(orch.create_task("系统自检"))
    run(orch.run_task(task))
    types = [e.type for e in bus.history(task.task_id)]
    assert types.count("tool.called") == types.count("tool.result") == 3


def test_failed_step_marks_task_failed(staged, monkeypatch) -> None:
    """工具抛错时，任务必须落到 failed 而不是停在 running。

    停在 running 的任务界面上永远转圈，而没有任何地方会报错。
    """
    from app.tools.registry import REGISTRY as TOOLS, ToolError

    orch, repo, bus, _ = staged
    spec = TOOLS.get("system.db_stats")

    async def boom(*a, **kw):
        raise ToolError("模拟工具失败")

    monkeypatch.setattr(TOOLS, "call", boom)
    task = orch.plan_task(orch.create_task("系统自检"))
    with pytest.raises(ToolError):
        run(orch.run_task(task))

    final = repo.get_task(task.task_id)
    assert final.status is TaskStatus.FAILED
    assert final.steps[0].status is StepStatus.FAILED
    assert "模拟工具失败" in (final.steps[0].error or "")

    types = [e.type for e in bus.history(task.task_id)]
    assert "step.failed" in types and "task.failed" in types
    # 失败的调用同样要留痕，而且状态是 failed
    calls = repo.list_tool_calls(task.task_id)
    assert calls and calls[-1].status == "failed"


def test_prior_results_flow_into_later_steps(staged) -> None:
    """汇总步骤必须拿到前面各步的结构化结果。

    它靠这个判断自检到底过没过。拿不到的话汇总会「看起来也在跑」，
    但永远报「通过」——一个恒为真的结论比没有结论更糟。
    """
    orch, _, bus, _ = staged
    task = orch.plan_task(orch.create_task("系统自检"))
    task = run(orch.run_task(task))
    summary = task.steps[-1].output_ref or ""
    assert "自检通过" in summary
    # 汇总里带上了第一步的数据库统计，说明 prior 真的传到了
    assert "42" in summary and "91" in summary


def test_task_cannot_be_run_twice(staged) -> None:
    """终态任务不能重跑。"""
    orch, _, _, _ = staged
    task = orch.plan_task(orch.create_task("系统自检"))
    task = run(orch.run_task(task))
    with pytest.raises(OrchestratorError):
        run(orch.run_task(task))


# ---------------------------------------------------------------- 事件总线


def test_subscribe_replays_history_after_last_event_id(staged) -> None:
    """断线重连：带 Last-Event-ID 订阅时，先补发缺的那几帧。

    这是「时间线不丢帧」的关键——一次网络抖动不该让演示现场少一格。
    """
    orch, _, bus, _ = staged
    task = orch.plan_task(orch.create_task("系统自检"))
    task = run(orch.run_task(task))
    total = len(bus.history(task.task_id))

    async def collect():
        out = []
        async for e in bus.subscribe(task.task_id, last_event_id=3):
            out.append(e)
            if len(out) >= total - 3:
                break
        return out

    replayed = run(collect())
    assert [e.seq for e in replayed] == list(range(4, total + 1))


def test_history_is_bounded_and_reports_truncation() -> None:
    """历史有上限；被截断时必须**明说**，不能静默丢弃。

    静默丢弃制造出的是一条看起来完整、实则缺了开头的时间线——
    比明确报错糟得多。
    """
    clock = FixedClock()
    bus = EventBus(history_limit=5, clock=clock)
    for i in range(10):
        bus.publish("t-x", "progress", {"i": i})
    assert len(bus.history("t-x")) == 5

    async def collect():
        out = []
        async for e in bus.subscribe("t-x", last_event_id=1):
            out.append(e)
            if len(out) >= 2:
                break
        return out

    got = run(collect())
    assert got[0].type == "warning"
    assert "不完整" in got[0].payload["message"]


def test_forget_refuses_while_subscribers_are_active(staged) -> None:
    """有活跃订阅者时不能释放历史，否则正在看的流会凭空断掉。"""
    _, _, bus, _ = staged
    bus.publish("t-y", "progress", {})

    async def hold():
        gen = bus.subscribe("t-y")
        await gen.__anext__()
        with pytest.raises(RuntimeError, match="订阅者"):
            bus.forget("t-y")
        await gen.aclose()

    run(hold())


def test_sse_frame_carries_id_and_event_name(staged) -> None:
    """SSE 报文必须带 `id:` 和 `event:`。

    没有 `id:` 浏览器就不会在重连时带 Last-Event-ID，补发无从谈起；
    没有 `event:` 前端只能在 message 回调里做 switch，容易漏。
    """
    _, _, bus, _ = staged
    e = bus.publish("t-z", "step.started", {"seq": 1}, step_id="s1")
    frame = format_sse(e)
    assert frame.startswith(f"id: {e.seq}\n")
    assert "event: step.started\n" in frame
    assert frame.endswith("\n\n")


# ---------------------------------------------------------------- 状态机集成


def test_illegal_transition_surfaces_from_the_orchestrator(staged) -> None:
    """状态机真的接在编排路径上，而不是一个没人调用的模块。"""
    with pytest.raises(IllegalTransition):
        assert_task_transition(TaskStatus.SUCCEEDED, TaskStatus.RUNNING)
