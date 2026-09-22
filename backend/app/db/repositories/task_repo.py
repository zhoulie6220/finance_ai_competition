"""任务、步骤、工具调用的读写。

时间一律由调用方传入（`now` 参数或 `clock`），**不在这里读系统时间**——
编排层可以注入固定时钟，整条时间线就是逐字节可复现的。

JSON 列（`plan` / `depends_on` / `args` / `payload`）在库里是 TEXT，进出都要转。
转换只发生在本文件，别让 `json.loads` 散落到业务代码里：一处忘了转，
拿到的就是一个字符串，而 `for x in "abc"` 会安安静静地循环三次。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from app.schemas.task import Task, TaskStep, ToolCall
from app.schemas.enums import StepStatus, TaskStatus, ToolTransport


def _loads(raw: str | None, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 坏 JSON 不静默吞掉：宁可让这行读出来是 None 并留下痕迹，
        # 也不要让调用方以为拿到了一份完整计划。
        return default


class TaskRepository:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    # ------------------------------------------------------------------ task

    def create_task(
        self,
        *,
        task_id: str,
        user_input: str,
        now: str,
        project_id: str | None = None,
        intent: str | None = None,
        skill_key: str | None = None,
    ) -> Task:
        self.con.execute(
            "INSERT INTO task (task_id, project_id, user_input, intent, skill_key,"
            " status, created_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, project_id, user_input, intent, skill_key,
             TaskStatus.PENDING.value, now),
        )
        return self.get_task(task_id)

    def project_exists(self, project_id: str) -> bool:
        """项目是否存在。

        给调用方一个**在写入之前**问一句的机会。直接 INSERT 让外键去拒的话，
        抛出来的是 sqlite 原文，没人能从里面读出「是哪个 id 不对」。
        """
        return self.con.execute(
            "SELECT 1 FROM project WHERE project_id=?", (project_id,)
        ).fetchone() is not None

    def get_task(self, task_id: str) -> Task:
        row = self.con.execute(
            "SELECT * FROM task WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"任务不存在：{task_id}")
        steps = self.list_steps(task_id)
        return Task(
            task_id=row["task_id"],
            project_id=row["project_id"],
            user_input=row["user_input"],
            intent=row["intent"],
            skill_key=row["skill_key"],
            plan=_loads(row["plan"], None),
            plan_confirmed=bool(row["plan_confirmed"]),
            status=TaskStatus(row["status"]),
            llm_plan_call_id=row["llm_plan_call_id"],
            error=row["error"],
            manifest_id=row["manifest_id"],
            steps=steps,
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def list_tasks(self, *, limit: int = 50) -> list[Task]:
        rows = self.con.execute(
            "SELECT task_id FROM task ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self.get_task(r["task_id"]) for r in rows]

    def set_task_status(
        self, task_id: str, status: TaskStatus, *, now: str, error: str | None = None
    ) -> None:
        """改任务状态。合法性的判定在编排器（`app/agents/state.py`），不在这里。

        仓储层只负责写。把状态机也塞进来的话，这条 SQL 就没法在数据修复、
        测试夹具等场景里直接用了。
        """
        sets = ["status=?"]
        vals: list[Any] = [status.value]
        if status == TaskStatus.RUNNING:
            sets.append("started_at=COALESCE(started_at, ?)")
            vals.append(now)
        if status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            sets.append("finished_at=?")
            vals.append(now)
        if error is not None:
            sets.append("error=?")
            vals.append(error)
        vals.append(task_id)
        self.con.execute(f"UPDATE task SET {', '.join(sets)} WHERE task_id=?", vals)

    def save_plan(
        self,
        task_id: str,
        *,
        plan: dict[str, Any],
        intent: str,
        skill_key: str,
        confirmed: bool = False,
    ) -> None:
        self.con.execute(
            "UPDATE task SET plan=?, intent=?, skill_key=?, plan_confirmed=? WHERE task_id=?",
            (json.dumps(plan, ensure_ascii=False), intent, skill_key,
             1 if confirmed else 0, task_id),
        )

    def confirm_plan(self, task_id: str) -> None:
        self.con.execute(
            "UPDATE task SET plan_confirmed=1 WHERE task_id=?", (task_id,)
        )

    # ------------------------------------------------------------------ step

    def add_steps(self, task_id: str, steps: Iterable[dict[str, Any]]) -> list[TaskStep]:
        """批量插入步骤。`depends_on` 传的是**下标**，这里翻译成真实 step_id。"""
        steps = list(steps)
        ids = [f"{task_id}-s{i + 1}" for i in range(len(steps))]
        for i, s in enumerate(steps):
            self.con.execute(
                "INSERT INTO task_step (step_id, task_id, seq, name, skill_key,"
                " tool_name, status, depends_on) VALUES (?,?,?,?,?,?,?,?)",
                (
                    ids[i], task_id, i + 1, s["name"], s.get("skill_key"),
                    s.get("tool_name"), StepStatus.PENDING.value,
                    json.dumps([ids[d] for d in s.get("depends_on", ())]),
                ),
            )
        return self.list_steps(task_id)

    def list_steps(self, task_id: str) -> list[TaskStep]:
        rows = self.con.execute(
            "SELECT * FROM task_step WHERE task_id=? ORDER BY seq", (task_id,)
        ).fetchall()
        return [
            TaskStep(
                step_id=r["step_id"],
                task_id=r["task_id"],
                seq=r["seq"],
                name=r["name"],
                skill_key=r["skill_key"],
                tool_name=r["tool_name"],
                status=StepStatus(r["status"]),
                depends_on=_loads(r["depends_on"], []),
                input_ref=_loads(r["input_ref"], None),
                output_ref=r["output_ref"],
                error=r["error"],
                retry_of=r["retry_of"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
            )
            for r in rows
        ]

    def set_step_status(
        self,
        step_id: str,
        status: StepStatus,
        *,
        now: str,
        output_ref: str | None = None,
        error: str | None = None,
    ) -> None:
        sets = ["status=?"]
        vals: list[Any] = [status.value]
        if status == StepStatus.RUNNING:
            sets.append("started_at=COALESCE(started_at, ?)")
            vals.append(now)
        if status in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED):
            sets.append("finished_at=?")
            vals.append(now)
        if output_ref is not None:
            sets.append("output_ref=?")
            vals.append(output_ref)
        if error is not None:
            sets.append("error=?")
            vals.append(error)
        vals.append(step_id)
        self.con.execute(f"UPDATE task_step SET {', '.join(sets)} WHERE step_id=?", vals)

    # ------------------------------------------------------------------ tool_call

    def record_tool_call(self, call: ToolCall) -> None:
        self.con.execute(
            "INSERT INTO tool_call (call_id, task_id, step_id, tool_name, tool_version,"
            " transport, deterministic, args, result_summary, result_hash, status,"
            " duration_ms, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                call.call_id, call.task_id, call.step_id, call.tool_name,
                call.tool_version, call.transport.value, 1 if call.deterministic else 0,
                json.dumps(call.args, ensure_ascii=False, default=str),
                call.result_summary, call.result_hash, call.status,
                call.duration_ms, call.error, call.created_at,
            ),
        )

    def list_tool_calls(self, task_id: str) -> list[ToolCall]:
        """任务的工具调用记录。

        `args` 在库里是 JSON 文本，**必须在这里转回 dict**（见模块开头那条约定）。
        原样返回 `dict(row)` 的话，接口会返回一个字符串，而 `ToolCall.args` 声明的
        是对象——前端写 `call.args.include_llm` 拿到 `undefined`，不报错，
        只是那个过滤条件静默失效。
        """
        rows = self.con.execute(
            "SELECT * FROM tool_call WHERE task_id=? ORDER BY created_at, rowid",
            (task_id,),
        ).fetchall()
        return [
            ToolCall(
                call_id=r["call_id"],
                task_id=r["task_id"],
                step_id=r["step_id"],
                tool_name=r["tool_name"],
                tool_version=r["tool_version"],
                transport=ToolTransport(r["transport"]),
                deterministic=bool(r["deterministic"]),
                args=_loads(r["args"], {}),
                result_summary=r["result_summary"],
                result_hash=r["result_hash"],
                status=r["status"],
                duration_ms=r["duration_ms"],
                error=r["error"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def new_call_id(self, task_id: str) -> str:
        n = self.con.execute(
            "SELECT COUNT(*) FROM tool_call WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        return f"{task_id}-c{n + 1}"

    # ------------------------------------------------------------------ app_log

    def log(
        self,
        *,
        ts: str,
        level: str,
        logger: str,
        event: str,
        message: str,
        task_id: str | None = None,
        step_id: str | None = None,
        request_id: str | None = None,
        payload: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self.con.execute(
            "INSERT INTO app_log (ts, level, logger, event, request_id, task_id,"
            " step_id, message, payload, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ts, level, logger, event, request_id, task_id, step_id, message,
                json.dumps(payload, ensure_ascii=False, default=str) if payload else None,
                duration_ms,
            ),
        )


__all__ = ["TaskRepository", "ToolTransport"]
