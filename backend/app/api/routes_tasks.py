"""任务路由：创建、查询、执行、SSE 事件流。

**SSE 事件契约是「冻结的接缝」**（见 CLAUDE.md「冻结的接缝」一节）：
事件类型取自 `app/agents/state.py::EVENT_TYPES`，前端按类型分发。
新增事件类型要同时改三处（state.py、app/schemas/task.py 的说明、前端处理），
否则新事件会**静默不显示**——时间线上少一格，看起来和"这一步本来就没有"一模一样。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.orchestrator import OrchestratorError
from app.agents.state import Clock
from app.api.deps import build_orchestrator, get_bus, get_clock, get_repo
from app.api.errors import COMMON_RESPONSES
from app.api.events import EventBus, format_sse
from app.config import get_settings
from app.db.repositories.task_repo import TaskRepository
from app.db.session import connect
from app.schemas.api import (
    EventHistoryResponse,
    TaskListResponse,
    TaskResponse,
    TaskStepView,
    ToolCallListResponse,
)
from app.schemas.enums import TaskStatus
from app.schemas.task import Task, TaskStep

router = APIRouter(prefix="/api/tasks", tags=["任务"], responses=COMMON_RESPONSES)


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(title="新建任务请求")

    # ⚠ 每个字段都写了 example，不是装饰。
    #   /docs 上的「Try it out」会把 example 当成**预填值**写进请求体；
    #   不写的话 Swagger 填的是 "string"，用户直接点 Execute 就把它当真实值发出去，
    #   于是撞上外键约束、得到一个 500 —— 一个「照着界面提示做」却失败的操作。
    input: str = Field(min_length=1, description="用户原话", examples=["跑一次系统自检"])
    project_id: str | None = Field(
        default=None,
        description="归属项目；留空表示不挂项目。填了就必须是 /api/projects 里真实存在的 id",
        examples=[None],
    )
    skill: str | None = Field(
        default=None,
        description="显式指定 Skill；不填则由规则路由选择",
        examples=[None],
    )
    run: bool = Field(default=True, description="创建后是否立即执行")
    sync: bool = Field(
        default=False,
        description="True=等任务跑完再返回（测试与命令行用）；False=后台执行，进度走 SSE",
    )


@router.post(
    "",
    summary="新建任务",
    response_model=TaskResponse,
    responses={
        400: {
            "description": "请求不合法：项目不存在、Skill 未登记、或没有 Skill 能处理这个请求"
        }
    },
)
async def create_task(
    body: CreateTaskRequest,
    repo: TaskRepository = Depends(get_repo),
    bus: EventBus = Depends(get_bus),
    clock: Clock = Depends(get_clock),
) -> dict[str, Any]:
    orch = build_orchestrator(repo, bus, clock)
    try:
        task = orch.create_task(body.input, project_id=body.project_id)
        if body.skill:
            from app.skills.base import REGISTRY as SKILLS

            skill = SKILLS.get(body.skill)
            task = orch.plan_task(task, skill=skill)
        else:
            task = orch.plan_task(task)
    except (OrchestratorError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        # 兜底：任何没被上面的显式检查挡住的约束冲突，都不该以 500 的样子出现。
        # 500 会把人送去查接口，而问题其实在这条请求的数据上。
        raise HTTPException(status_code=400, detail=f"数据不满足约束：{exc}") from exc

    repo.con.commit()

    if not body.run:
        return _task_payload(task)

    if body.sync:
        # 同步模式要自己开一条连接：请求级连接会在这个函数返回时关掉，
        # 而后台协程还要用它写库。
        #
        # 任务失败**不**转成 HTTP 错误码：请求本身是成功的（任务建出来了），
        # 失败的是任务。返回 200 + status=failed + error 让前端按任务失败渲染，
        # 比甩一个 500 更准确——500 会让人以为是接口坏了，去查接口而不是查任务。
        await _run_in_background(task.task_id, bus, clock)
        return _task_payload(_load_task(task.task_id))

    asyncio.create_task(_run_in_background(task.task_id, bus, clock))
    return _task_payload(task)


async def _run_in_background(task_id: str, bus: EventBus, clock: Clock) -> None:
    """在自己的连接上跑任务。

    必须自己开连接：请求级依赖的连接在响应返回时就关了，
    复用它会让后台写库报 `Cannot operate on a closed database`——
    而且是在任务已经跑了一半之后才报。
    """
    con = connect()
    try:
        repo = TaskRepository(con)
        orch = build_orchestrator(repo, bus, clock)
        task = repo.get_task(task_id)
        try:
            await orch.run_task(task)
        except Exception as exc:  # noqa: BLE001
            # ⚠ 这里**提交**而不是回滚。
            #
            # 编排器在失败路径上已经写好了「任务 failed、哪一步 failed、
            # 工具报了什么错」，并推过事件。回滚会把这些一起抹掉，
            # 页面上只剩一个永远停在 running 的任务，而**没有任何地方会报错**——
            # 对一套以「执行过程可追溯」为卖点的系统来说，
            # 丢掉失败记录比留下一次部分写入严重得多。
            repo.log(
                ts=clock.now(), level="ERROR", logger="orchestrator",
                event="task.run_error", task_id=task_id,
                message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            con.commit()
    except Exception as exc:  # noqa: BLE001 —— 连仓储都起不来时才走到这里
        con.rollback()
        bus.publish(task_id, "task.failed", {"error": f"后台执行异常：{exc}"})
        bus.close(task_id)
    finally:
        con.close()


def _load_task(task_id: str) -> Task:
    con = connect()
    try:
        return TaskRepository(con).get_task(task_id)
    finally:
        con.close()


def _task_is_finished(task_id: str) -> bool:
    """任务是否已到终态。查不到就当作未结束——让它挂一会儿，
    总好过把一个还在跑的任务流提前关掉。"""
    from app.agents.state import is_task_terminal

    try:
        return is_task_terminal(_load_task(task_id).status)
    except Exception:  # noqa: BLE001
        return False


def _task_payload(task: Task) -> dict[str, Any]:
    """任务详情。

    `task` 原样传进去即可 —— `TaskSummary` 会把 `steps` 排掉（`exclude=True`），
    步骤走下面这个裁剪过的视图。这里不手写字段列表：手写的话，将来给 `TaskStep`
    加字段会漏掉这一处，接口上凭空少一个字段而**没有任何地方会报错**。
    """
    return {"task": task, "steps": [_step_view(s) for s in task.steps]}


def _step_view(s: TaskStep) -> TaskStepView:
    return TaskStepView(
        step_id=s.step_id,
        seq=s.seq,
        name=s.name,
        tool=s.tool_name,
        status=s.status.value,
        depends_on=s.depends_on,
        output=s.output_ref,
        error=s.error,
    )


@router.get("", summary="任务列表", response_model=TaskListResponse)
def list_tasks(
    limit: int = Query(default=50, ge=1, le=200, description="最多返回多少条"),
    repo: TaskRepository = Depends(get_repo),
) -> dict[str, Any]:
    return {"tasks": repo.list_tasks(limit=limit)}


@router.get(
    "/{task_id}",
    summary="任务详情",
    response_model=TaskResponse,
    responses={404: {"description": "任务不存在"}},
)
def get_task(task_id: str, repo: TaskRepository = Depends(get_repo)) -> dict[str, Any]:
    try:
        task = repo.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _task_payload(task)


@router.get(
    "/{task_id}/tool-calls",
    summary="任务的工具调用记录",
    response_model=ToolCallListResponse,
    responses={404: {"description": "任务不存在"}},
)
def task_tool_calls(
    task_id: str, repo: TaskRepository = Depends(get_repo)
) -> dict[str, Any]:
    """「工具调用情况完整记录」的查询入口。"""
    try:
        repo.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    calls = repo.list_tool_calls(task_id)
    return {"count": len(calls), "calls": calls}


@router.post(
    "/{task_id}/retry",
    summary="重跑失败的任务",
    response_model=TaskResponse,
    responses={
        404: {"description": "任务不存在"},
        409: {"description": "任务不处于可重跑的状态"},
    },
)
async def retry_task(
    task_id: str,
    repo: TaskRepository = Depends(get_repo),
    bus: EventBus = Depends(get_bus),
    clock: Clock = Depends(get_clock),
) -> dict[str, Any]:
    """重跑一个等待确认或失败的任务。

    只做「回到 pending 再走一遍」，不复制步骤：步骤表里已有的记录是**历史**，
    抹掉它们等于抹掉「这个任务失败过一次」这个事实。
    """
    try:
        task = repo.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if task.status == TaskStatus.WAITING_CONFIRM:
        repo.confirm_plan(task_id)
        repo.set_task_status(task_id, TaskStatus.PLANNED, now=clock.now())
    elif task.status == TaskStatus.FAILED:
        repo.set_task_status(task_id, TaskStatus.PLANNED, now=clock.now())
    else:
        raise HTTPException(
            status_code=409,
            detail=f"任务处于 {task.status.value}，只有 failed 或 waiting_confirm 可以重跑",
        )
    repo.con.commit()

    asyncio.create_task(_run_in_background(task_id, bus, clock))
    return _task_payload(_load_task(task_id))


@router.get(
    "/{task_id}/events",
    summary="任务事件流（SSE）",
    response_class=StreamingResponse,
    # 这个接口不返回 JSON，得显式声明成事件流，否则 /docs 会把它显示成一个
    # 点开就转圈的 JSON 接口——而它其实是一条要一直挂着的长连接。
    responses={
        200: {
            "description": "SSE 事件流；任务到终态后自动关闭",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def task_events(
    task_id: str,
    request: Request,
    bus: EventBus = Depends(get_bus),
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
    last_event_id: int | None = Query(default=None, alias="lastEventId"),
) -> StreamingResponse:
    """任务时间线。

    断线重连时浏览器自动带上 `Last-Event-ID` 头，服务端从该序号之后补发，
    再接实时流——时间线不会因为一次断网就少一格。
    """
    resume_from = last_event_id
    if resume_from is None and last_event_id_header:
        try:
            resume_from = int(last_event_id_header)
        except ValueError:
            resume_from = None

    settings = get_settings()
    # 任务已经结束的话，补发完历史就关流，不挂在那里等心跳
    finished = _task_is_finished(task_id)

    async def stream() -> AsyncIterator[str]:
        # 先发一条注释行把响应头冲出去。某些代理会缓冲到攒够一定字节才转发，
        # 没有这一行的话，任务前几秒的事件会看起来"没发出来"。
        yield ": connected\n\n"
        async for event in bus.subscribe(
            task_id,
            last_event_id=resume_from,
            heartbeat_seconds=settings.sse_heartbeat_seconds,
            already_finished=finished,
        ):
            if await request.is_disconnected():
                return
            yield format_sse(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx 用这个头决定不做缓冲；本地直连时无害
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/{task_id}/events/history",
    summary="任务事件历史（一次性返回）",
    response_model=EventHistoryResponse,
)
def task_events_history(
    task_id: str, bus: EventBus = Depends(get_bus)
) -> dict[str, Any]:
    """已经发出的事件。

    给两个场景用：① 页面刷新后先拉一次历史再订阅实时流，避免等待期间空白；
    ② 排查「前端少了一格」时，对照服务端到底发了什么。
    """
    events = bus.history(task_id)
    return {
        "count": len(events),
        "events": [e.model_dump(mode="json") for e in events],
    }
