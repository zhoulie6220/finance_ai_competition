"""编排器：一句话进来，一条时间线出去。

    create_task()   建任务（pending）
    plan_task()     选 Skill、拆步骤、落库（→ planned），发 plan.created
    run_task()      逐步执行：过状态机 → 调 Tool → 落库 → 发事件（→ succeeded）

赛事要求「执行过程可追溯、运行结果可复现」，本文件是那条要求的执行者。三条规矩：

1. **先规划再执行。** 计划非空且写进了 `task.plan` 才开跑，不允许边想边做——
   边做边想的任务，事后无法回答「它当时打算干什么」。
2. **落库与推事件是同一个动作。** 只推不落，刷新页面时间线就空了；
   只落不推，页面停在原地。所以两者都走 `_transition_*` 这一对方法。
3. **状态机说了算。** 每次改状态都过 `assert_*_transition`，非法转移当场抛异常。

`guards.py`（数字守卫）还没落地，挂载点已经标在 `_run_step` 里了。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from app.agents.state import (
    Clock,
    assert_step_transition,
    assert_task_transition,
    is_step_terminal,
)
from app.api.events import EventBus
from app.db.repositories.task_repo import TaskRepository
from app.schemas.enums import StepStatus, TaskStatus, ToolTransport
from app.schemas.task import Task, TaskStep, ToolCall
from app.skills.base import PlannedStep, Skill, SkillRequest, StepOutcome
from app.tools.registry import REGISTRY as TOOLS
from app.tools.registry import ToolError, result_hash


class OrchestratorError(RuntimeError):
    pass


@dataclass
class StepContext:
    """一步的执行环境。

    工具与 Skill 通过它拿到时钟、仓储和任务标识，**而不是** import 全局的
    数据库连接——依赖显式化，测试时能整个换掉。
    """

    repo: TaskRepository
    bus: EventBus
    clock: Clock
    task_id: str
    step_id: str
    project_id: str | None = None
    offline: bool = False

    def now(self) -> str:
        return self.clock.now()


@dataclass
class Orchestrator:
    repo: TaskRepository
    bus: EventBus
    clock: Clock
    skills: Any  # SkillRegistry；用 Any 是为了不让 skills 包反向 import agents
    offline: bool = False
    _running: dict[str, Task] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ 建任务

    def create_task(
        self, user_input: str, *, project_id: str | None = None
    ) -> Task:
        if not user_input.strip():
            raise OrchestratorError("请求内容不能为空")
        # 项目不存在就在**建任务之前**拒绝。
        #
        # 不挡的话 INSERT 会撞上 task.project_id 的外键约束，抛出来的是一句 sqlite
        # 原文（"FOREIGN KEY constraint failed"），被兜底处理器包成 500。用户看到的
        # 是「服务器内部错误」，而真正的原因是「项目 id 不存在」——
        # 和 CLAUDE.md 里那条「不要带着空库启动」是同一个道理：
        # 报错要指向真正的原因，否则排查方向从一开始就是错的。
        if project_id is not None and not self.repo.project_exists(project_id):
            raise OrchestratorError(f"项目不存在：{project_id}")
        task_id = f"t-{uuid.uuid4().hex[:12]}"
        task = self.repo.create_task(
            task_id=task_id, user_input=user_input, now=self.clock.now(),
            project_id=project_id,
        )
        self.repo.log(
            ts=self.clock.now(), level="INFO", logger="orchestrator",
            event="task.created", message=f"新建任务：{user_input}",
            task_id=task_id,
        )
        return task

    # ------------------------------------------------------------------ 规划

    def plan_task(self, task: Task, skill: Skill | None = None) -> Task:
        request = SkillRequest(user_input=task.user_input, project_id=task.project_id)

        skill = skill or self.skills.route(request)
        if skill is None:
            self._fail(task, "没有 Skill 能处理这个请求（规则路由未命中，LLM 兜底尚未接入）")
            raise OrchestratorError(f"无法为请求选择 Skill：{task.user_input!r}")

        planned = list(skill.plan(request))
        if not planned:
            self._fail(task, f"Skill {skill.key} 没有产出任何步骤")
            raise OrchestratorError(f"Skill {skill.key} 的计划为空")

        self._transition_task(task, TaskStatus.PLANNED)
        self.repo.add_steps(
            task.task_id,
            [
                {
                    "name": s.name,
                    "skill_key": skill.key,
                    "tool_name": s.tool_name,
                    "depends_on": s.depends_on,
                }
                for s in planned
            ],
        )
        # 计划的完整内容（含参数）存进 task.plan：steps 表存的是执行状态，
        # plan 存的是「当时打算怎么做」。两者分开是因为参数里可能有
        # 几百个字节的 JSON，塞进步骤表会让时间线查询变重。
        self.repo.save_plan(
            task.task_id,
            plan={
                "skill": skill.key,
                "steps": [
                    {
                        "seq": i + 1,
                        "name": s.name,
                        "tool": s.tool_name,
                        "args": s.args,
                        "depends_on": list(s.depends_on),
                        "requires_review": s.requires_review,
                    }
                    for i, s in enumerate(planned)
                ],
            },
            intent=skill.key,
            skill_key=skill.key,
            confirmed=not any(s.requires_review for s in planned),
        )

        task = self.repo.get_task(task.task_id)
        self.bus.publish(
            task.task_id,
            "plan.created",
            {
                "skill": skill.key,
                "skill_name": skill.name_cn,
                "steps": [
                    {"seq": s.seq, "name": s.name, "tool": s.tool_name}
                    for s in task.steps
                ],
                "plan_confirmed": task.plan_confirmed,
            },
        )
        return task

    # ------------------------------------------------------------------ 执行

    async def run_task(self, task: Task) -> Task:
        """按计划逐步执行。步骤串行——本项目的数据量（约 900 行事实）不值得并行，
        而并行的写库顺序会让时间线不再是确定的。"""
        if task.status not in (TaskStatus.PLANNED, TaskStatus.WAITING_CONFIRM):
            raise OrchestratorError(
                f"任务 {task.task_id} 处于 {task.status.value}，不能执行"
            )
        if not task.plan_confirmed:
            raise OrchestratorError(
                f"任务 {task.task_id} 的计划尚未确认。"
                "含人工复核步骤的计划必须确认后才能执行——这是「先规划再执行」的强制点"
            )

        self._running[task.task_id] = task
        self._transition_task(task, TaskStatus.RUNNING)
        skill = self.skills.get(task.skill_key or "")
        planned = self._planned_steps(task)

        # 本次运行中已成功步骤的结果，按计划下标索引，传给后面的步骤做汇总。
        # 只活在这一次 run_task 的栈上——不能存进任务对象，
        # 否则重跑任务时会读到上一轮的残留。
        prior: dict[int, StepOutcome] = {}

        try:
            for step in task.steps:
                if is_step_terminal(step.status):
                    continue
                idx = step.seq - 1
                plan = planned.get(idx)
                if plan is None:
                    self._skip(step, "计划里没有对应的步骤定义")
                    continue
                outcome = await self._run_step(task, step, plan, skill, prior)
                if outcome is None:
                    continue
                if outcome.requires_review:
                    self._transition_task(task, TaskStatus.WAITING_CONFIRM)
                    self.bus.publish(
                        task.task_id, "review.required",
                        {"step_id": step.step_id, "step": step.name,
                         "prompt": outcome.review_prompt},
                        step_id=step.step_id,
                    )
                    return self.repo.get_task(task.task_id)
                prior[idx] = outcome
        except Exception as exc:  # noqa: BLE001 —— 任何异常都要落到任务状态上
            self._fail(task, str(exc))
            raise

        self._transition_task(task, TaskStatus.SUCCEEDED)
        final = self.repo.get_task(task.task_id)
        self.bus.publish(
            task.task_id, "task.completed",
            {"status": final.status.value, "steps": len(final.steps)},
        )
        self.bus.close(task.task_id)
        self._running.pop(task.task_id, None)
        return final

    async def _run_step(
        self,
        task: Task,
        step: TaskStep,
        plan: PlannedStep,
        skill: Skill,
        prior: dict[int, StepOutcome],
    ) -> StepOutcome | None:
        """跑一步。返回 None 表示这一步没有产出（跳过）。"""
        # 上一次失败的步骤必须经 retrying 中转才能再跑。
        # 直接从 failed 跳回 running 会让「这一步重试过」在时间线上消失，
        # 而重试次数恰好是评审判断系统稳不稳的一个凭据。
        if step.status is StepStatus.FAILED:
            self._transition_step(step, StepStatus.RETRYING)
            self.bus.publish(
                task.task_id, "step.retrying",
                {"seq": step.seq, "name": step.name, "previous_error": step.error},
                step_id=step.step_id,
            )

        self._transition_step(step, StepStatus.RUNNING)
        self.bus.publish(
            task.task_id, "step.started",
            {"seq": step.seq, "name": step.name, "tool": step.tool_name},
            step_id=step.step_id,
        )

        ctx = StepContext(
            repo=self.repo, bus=self.bus, clock=self.clock,
            task_id=task.task_id, step_id=step.step_id,
            project_id=task.project_id, offline=self.offline,
        )

        try:
            if plan.tool_name:
                tool_outcome = await self._call_tool(task, step, plan, ctx)
                outcome = StepOutcome(
                    summary=tool_outcome.summary,
                    value=tool_outcome.value,
                    evidence_refs=list(tool_outcome.evidence_refs),
                )
                if tool_outcome.formula:
                    # 公式与入参一并带出去，这是「点击结论回到计算过程」的路径。
                    # 用 replace 而不是赋值：StepOutcome 是 frozen 的，
                    # 冻结是刻意的——步骤结果一旦产生就不该被就地改写。
                    outcome = replace(outcome, value={
                        "result": _jsonable(tool_outcome.value),
                        "formula": tool_outcome.formula,
                        "inputs": _jsonable(tool_outcome.inputs),
                    })
            else:
                # 没指定工具的步骤由 Skill 自己处理（汇总、组织文字）
                outcome = await skill.run(plan, ctx, prior)
                # ★ guards.py 的挂载点：Skill 产出的文字里若含数字，
                #   这里要过 assert_numbers_grounded，确保每个数字都在本次
                #   工具结果里找得到。守卫落地前，含 LLM 的 Skill 不得直接进主流程。
        except ToolError as exc:
            self._transition_step(step, StepStatus.FAILED, error=str(exc))
            self.bus.publish(
                task.task_id, "step.failed",
                {"seq": step.seq, "name": step.name, "error": str(exc)},
                step_id=step.step_id,
            )
            raise

        for ref in outcome.evidence_refs:
            self.bus.publish(
                task.task_id, "evidence.attached", {"ref": ref}, step_id=step.step_id
            )

        # 人工复核优先于成功：一步既要人确认、又被记成 succeeded，
        # 时间线上就会显示成「已完成」，没人会去点那个确认按钮。
        if plan.requires_review or outcome.requires_review:
            self._transition_step(step, StepStatus.SKIPPED,
                                  error="等待人工确认", output_ref=outcome.summary)
            return StepOutcome(
                summary=outcome.summary,
                value=outcome.value,
                requires_review=True,
                review_prompt=outcome.review_prompt or f"请确认：{step.name}",
                evidence_refs=outcome.evidence_refs,
            )

        self._transition_step(step, StepStatus.SUCCEEDED, output_ref=outcome.summary)
        self.bus.publish(
            task.task_id, "step.succeeded",
            {"seq": step.seq, "name": step.name, "summary": outcome.summary,
             "value": _jsonable(outcome.value)},
            step_id=step.step_id,
        )
        return outcome

    async def _call_tool(
        self, task: Task, step: TaskStep, plan: PlannedStep, ctx: StepContext
    ):
        """调工具并把这次调用完整留痕。"""
        spec = TOOLS.get(plan.tool_name or "")
        call_id = self.repo.new_call_id(task.task_id)
        self.bus.publish(
            task.task_id, "tool.called",
            {"tool": spec.name, "version": spec.version,
             "deterministic": spec.deterministic, "args": plan.args},
            step_id=step.step_id,
        )

        # 取值受 `tool_call.status` 的 CHECK 约束：'succeeded' / 'failed' / 'timeout'。
        # 刻意不在这里自由命名——历史记录是给评委查的，取值必须可枚举、可统计。
        status, error, outcome, duration_ms, digest = "succeeded", None, None, None, None
        try:
            result = await TOOLS.call(spec, plan.args, ctx=ctx)
            outcome, duration_ms = result.outcome, result.duration_ms
            digest = result_hash(outcome.value)
            return outcome
        except ToolError as exc:
            status, error = "failed", str(exc)
            raise
        finally:
            # finally 而不是 except：**成功的调用同样要留痕**。
            # 只在失败时记录，`tool_call` 表就变成了错误日志，
            # 而「工具调用情况完整记录」要的是全部调用。
            self.repo.record_tool_call(
                ToolCall(
                    call_id=call_id, task_id=task.task_id, step_id=step.step_id,
                    tool_name=spec.name, tool_version=spec.version,
                    transport=ToolTransport.INTERNAL,
                    deterministic=spec.deterministic,
                    args=plan.args,
                    result_summary=(outcome.summary if outcome else (error or "失败")),
                    result_hash=digest, status=status, duration_ms=duration_ms,
                    error=error, created_at=self.clock.now(),
                )
            )
            self.bus.publish(
                task.task_id, "tool.result",
                {"tool": spec.name, "status": status, "duration_ms": duration_ms,
                 "summary": outcome.summary if outcome else error,
                 "deterministic": spec.deterministic},
                step_id=step.step_id,
            )

    # ------------------------------------------------------------------ 状态转移

    def _transition_task(self, task: Task, target: TaskStatus) -> None:
        assert_task_transition(task.status, target)
        self.repo.set_task_status(task.task_id, target, now=self.clock.now())
        task.status = target

    def _transition_step(
        self,
        step: TaskStep,
        target: StepStatus,
        *,
        error: str | None = None,
        output_ref: str | None = None,
    ) -> None:
        assert_step_transition(step.status, target)
        self.repo.set_step_status(
            step.step_id, target, now=self.clock.now(),
            error=error, output_ref=output_ref,
        )
        step.status = target

    def _skip(self, step: TaskStep, reason: str) -> None:
        self._transition_step(step, StepStatus.SKIPPED, error=reason)
        self.bus.publish(
            step.task_id, "step.skipped",
            {"seq": step.seq, "name": step.name, "reason": reason},
            step_id=step.step_id,
        )

    def _fail(self, task: Task, message: str) -> None:
        try:
            self._transition_task(task, TaskStatus.FAILED)
        except Exception:  # noqa: BLE001 —— 终态不能再转移，说明已经失败过了
            return
        self.repo.log(
            ts=self.clock.now(), level="ERROR", logger="orchestrator",
            event="task.failed", message=message, task_id=task.task_id,
        )
        self.bus.publish(task.task_id, "task.failed", {"error": message})
        self.bus.close(task.task_id)

    def _planned_steps(self, task: Task) -> dict[int, PlannedStep]:
        """把落库的计划还原成 `PlannedStep`。

        从 `task.plan` 还原而不是另存一份：计划只有一份真源，
        两处存就意味着两处可能不一致，而不一致时谁也说不清该信哪个。
        """
        plan = task.plan or {}
        out: dict[int, PlannedStep] = {}
        for item in plan.get("steps", []):
            out[item["seq"] - 1] = PlannedStep(
                name=item["name"],
                tool_name=item.get("tool"),
                args=item.get("args") or {},
                depends_on=tuple(item.get("depends_on") or ()),
                requires_review=bool(item.get("requires_review")),
            )
        return out


def _jsonable(value: Any) -> Any:
    """把结果转成能进 JSON 的形态。

    Decimal 直接 `json.dumps` 会抛 `TypeError`——那会发生在**推事件的时候**，
    也就是一步已经算完、事件却发不出去，时间线上看就是卡住。所以在推之前统一转。
    """
    import json

    try:
        json.dumps(value, default=str)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
