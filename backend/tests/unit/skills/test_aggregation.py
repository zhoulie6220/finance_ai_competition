"""汇总步骤必须读到**工具真正返回的那个值**。

这个文件来自一次真实翻车。编排器会把工具结果包一层再交给后续步骤：

    outcome.value = {"result": <工具返回值>, "formula": ..., "inputs": ...}

包这一层是对的——公式与入参是「点击结论回到计算过程」的路径。问题是这个包装
**对被包装者是隐形的**：自检 Skill 的汇总读 `value.get("passed")`，拿到 `None`，
而 `None is False` 为假，于是：

    字典检查报了 1 个问题  →  汇总输出「自检通过」

检查跑了、发现了、然后被静默吞掉。整个系统里没有任何地方会报错，
页面上那一行还写着「通过」。

所以这里盯的不是「汇总写得好不好看」，而是**它有没有真的在看结果**。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.agents.orchestrator import Orchestrator
from app.agents.state import FixedClock
from app.api.events import EventBus
from app.db.repositories.task_repo import TaskRepository
from app.db.session import connect_memory, init_schema, load_seeds
from app.skills.base import tool_result

import app.skills  # noqa: F401 —— 导入即注册
from app.skills.base import REGISTRY as SKILLS


@pytest.fixture()
def staged():
    con: sqlite3.Connection = connect_memory()
    init_schema(con)
    load_seeds(con)
    clock = FixedClock()
    repo = TaskRepository(con)
    orch = Orchestrator(repo=repo, bus=EventBus(clock=clock), clock=clock, skills=SKILLS)
    yield orch, repo, con
    con.close()


def _run_last_step(orch, repo, text: str, **kw) -> str:
    task = orch.create_task(text, **kw)
    orch.plan_task(task)
    task = repo.get_task(task.task_id)
    asyncio.run(orch.run_task(task))
    return repo.get_task(task.task_id).steps[-1].output_ref or ""


# ------------------------------------------------------------------ 包装契约


def test_tool_result_unwraps_and_tolerates_junk() -> None:
    """`tool_result()` 的形状契约。

    编排器把工具结果包成 `{"result": ..., "formula": ..., "inputs": ...}`，
    这个函数就是拆那一层。形状变了而它没跟着变的话，所有汇总都会读到空字典
    ——又一次静默失效。所以毁形的那几种情况都在这里钉死。
    """
    from app.skills.base import StepOutcome

    assert tool_result(None) == {}
    assert tool_result(StepOutcome(summary="x", value=None)) == {}
    assert tool_result(StepOutcome(summary="x", value={"result": {"a": 1}})) == {"a": 1}
    # result 不是 dict 时返回空字典，而不是把非 dict 原样抛给调用方
    assert tool_result(StepOutcome(summary="x", value={"result": "字符串"})) == {}
    # 没有包装层的形状（工具直接返回裸 dict）：返回空字典，**不猜**。
    # 猜的话会把 {"passed": False} 这种「工具结果」和「包装层」混淆。
    assert tool_result(StepOutcome(summary="x", value={"passed": False})) == {}


def _wrapped(payload: dict) -> dict:
    """编排器真实的包装形状。测试里照着它造输入，而不是造一个更宽松的形状。"""
    return {"result": payload, "formula": "（测试）", "inputs": {}}


# ------------------------------------------------------------------ 汇总行为


def test_summary_step_reads_through_the_wrapper(staged) -> None:
    """★ 直接调 Skill 的汇总步骤，喂给它**编排器真实的包装形状**。

    这是那条真实翻车的精确复现：修之前，汇总读 `value.get("passed")` 拿到
    `None`，`None is False` 为假，于是「字典检查失败」被汇总成「自检通过」。
    """
    from app.skills.base import PlannedStep, StepOutcome
    from app.skills.selfcheck import SelfCheckSkill

    skill = SelfCheckSkill()
    summary_step = PlannedStep(name="汇总自检结果")
    prior = {
        0: StepOutcome(summary="表 42 张", value=_wrapped({"tables": 42})),
        1: StepOutcome(summary="字段字典有 1 个问题", value=_wrapped(
            {"passed": False, "problems": ["别名重复：应收账款 挂在两个字段上"]}
        )),
        2: StepOutcome(summary="一致", value=_wrapped({"mismatches": []})),
    }
    out = asyncio.run(skill.run(summary_step, None, prior))  # type: ignore[arg-type]

    assert "自检通过" not in out.summary, f"检查失败却报了通过：{out.summary!r}"
    assert "别名重复" in out.summary, f"摘要里没说是哪个问题：{out.summary!r}"


def test_summary_step_reads_rule_config_mismatches(staged) -> None:
    """规则参数与引擎默认值分叉，同样要经包装层读出来。"""
    from app.skills.base import PlannedStep, StepOutcome
    from app.skills.selfcheck import SelfCheckSkill

    skill = SelfCheckSkill()
    prior = {
        0: StepOutcome(summary="表 42 张", value=_wrapped({"tables": 42})),
        1: StepOutcome(summary="通过", value=_wrapped({"passed": True, "problems": []})),
        2: StepOutcome(summary="⚠ 1 项不一致", value=_wrapped({"mismatches": [
            {"key": "normalization.phase_min_spread", "label": "高低盈利阶段最低落差",
             "config": "0.99", "engine": "0.02", "reason": "取值不一致"},
        ]})),
    }
    out = asyncio.run(skill.run(PlannedStep(name="汇总自检结果"), None, prior))  # type: ignore[arg-type]

    assert "自检通过" not in out.summary, f"参数分叉却报了通过：{out.summary!r}"
    assert "高低盈利阶段最低落差" in out.summary


# ------------------------------------------------------------------ 端到端


def test_selfcheck_reports_dictionary_problems(staged, monkeypatch) -> None:
    """同样的翻车，走完整编排链路再验一次。"""
    orch, repo, _ = staged
    import app.db.dictionary as dic

    monkeypatch.setattr(dic, "validate", lambda con: ["别名重复：应收账款 挂在两个字段上"])

    out = _run_last_step(orch, repo, "跑一次系统自检")
    assert "自检通过" not in out, f"检查失败却报了通过：{out!r}"
    assert "别名重复" in out


def test_selfcheck_still_passes_on_clean_data(staged) -> None:
    """反例的反例：数据没问题时不能乱报。

    只断言「能报错」是不够的——一个恒返回「有问题」的汇总也能通过上面两条。
    """
    orch, repo, _ = staged
    out = _run_last_step(orch, repo, "跑一次系统自检")
    assert "自检通过" in out, out


# ------------------------------------------------------------------ 财务事实 Skill


def test_facts_summary_reads_real_numbers(staged) -> None:
    """财务事实的汇总要真的读到序列。

    没有演示数据时它会说「库里还没有任何项目」——这不是失败，
    是如实回答；所以这里断言的是**不会出现「无数据」这种读错层的症状**。
    """
    orch, repo, con = staged
    out = _run_last_step(orch, repo, "看一下财务事实趋势")
    # 空库时应给出可操作的指引，而不是一句「无数据」
    assert "seed_demo" in out or "项目" in out, out


def test_facts_skill_routes_by_keyword(staged) -> None:
    """两个 Skill 的关键词不能重叠。

    路由取**第一个命中**的 Skill，重叠的话后一个永远轮不到，且不报错。
    """
    selfcheck = SKILLS.get("selfcheck")
    facts = SKILLS.get("facts")
    from app.skills.base import SkillRequest

    for text in ("财务事实", "看一下营收趋势", "净利润怎么样", "毛利率"):
        r = SkillRequest(user_input=text)
        assert facts.can_handle(r), f"{text!r} 没路由到 facts"
        assert not selfcheck.can_handle(r), f"{text!r} 被 selfcheck 抢走了"

    for text in ("跑一次系统自检", "体检"):
        r = SkillRequest(user_input=text)
        assert selfcheck.can_handle(r), f"{text!r} 没路由到 selfcheck"
        assert not facts.can_handle(r), f"{text!r} 被 facts 抢走了"
