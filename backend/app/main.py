"""FastAPI 入口。

    uvicorn app.main:app --reload

**前端只通过 REST + SSE 取数，不在浏览器里做任何业务计算。** 这条不是风格偏好：
赛事的硬要求是「计算可复算、过程可追溯」，浏览器里跑的算术无法审计——
动态语言的浮点结果没法复现，评审也没法核。所以所有数字都从这里出，
且金额一律是字符串。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from app.agents.llm.settings import LlmSettings
from app.api import router as api_router
from app.api.routes import get_con


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动时说一句话，把「库是空的」和「没配密钥」当场讲清楚。

    这两种状态都不会让服务起不来，但会让页面显示空白或功能不可用。
    不主动说，排查就得从头猜——而这两种恰恰是最容易被当成「前端坏了」的。
    """
    from app.db import repository
    from app.db.session import connect

    con = connect()
    try:
        counts = repository.db_counts(con)
        n = counts.get("financial_fact", 0)
    finally:
        con.close()

    settings = LlmSettings.from_env()
    print(f"[启动] 财务事实 {n} 条；模型：{settings.describe()}")
    if n == 0:
        print(
            "[启动] ⚠ 库里没有财务事实。重建之后需要导回数据包：\n"
            "        python scripts/merge_data_pack.py --source <数据包>/backend/var/finance.db"
        )
    yield


app = FastAPI(
    title="财报叙事一致性分析与情景估值投研工作台",
    description=(
        "A 股能源钢铁行业年报分析。数字由程序计算，模型只负责理解和组织文字；"
        "所有结论可点回到「文件 → 页码 → 原文」。"
    ),
    version="0.3.0",
    lifespan=lifespan,
)


def _cors_origins() -> list[str]:
    """允许的前端来源。从 .env 的 CORS_ORIGIN 读，默认本地开发地址。

    只允许配置里写明的来源，不用 `*`——`*` 配 `allow_credentials` 会被浏览器
    拒绝，而不配 credentials 又会让以后想加鉴权时踩坑。
    """
    import os

    raw = os.environ.get("CORS_ORIGIN", "http://localhost:5173")
    return [o.strip() for o in raw.split(",") if o.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


# ---------------------------------------------------------------- 统一错误格式


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    """统一错误体：{detail, status}。

    保留后端写的中文 detail 原样透出——那是给人看的（「这笔事实定位不到原文页，
    多半是 source_page 与 page_no 对不上」），前端直接展示即可，不要再包一层
    「请求失败」把它盖掉。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status": exc.status_code},
    )


# ---------------------------------------------------------------- 任务事件流（SSE）


@app.get("/api/tasks")
def list_tasks(
    project_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    con: sqlite3.Connection = Depends(get_con),
) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT task_id, project_id, status, user_input, created_at, finished_at"
        " FROM task WHERE (? IS NULL OR project_id = ?)"
        " ORDER BY created_at DESC LIMIT ?",
        (project_id, project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/tasks/{task_id}")
def get_task(
    task_id: str, con: sqlite3.Connection = Depends(get_con)
) -> dict[str, Any]:
    task = con.execute(
        "SELECT * FROM task WHERE task_id = ?", (task_id,)
    ).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail=f"没有这个任务：{task_id}")
    steps = con.execute(
        "SELECT step_id, seq, name, status, tool_name, started_at, finished_at,"
        " error, output_ref FROM task_step WHERE task_id = ? ORDER BY seq",
        (task_id,),
    ).fetchall()
    return {**dict(task), "steps": [dict(s) for s in steps]}


@app.get("/api/tasks/{task_id}/events")
async def task_events(
    task_id: str,
    poll_s: float = Query(default=0.3, ge=0.05, le=5.0),
) -> StreamingResponse:
    """任务时间线的 SSE 流。

    轮询 `task_step` 并对**增量**发事件——只发新出现的步骤，不每次全量重发。
    全量重发在步骤多起来之后会让前端反复重排，看起来像卡顿。

    每 15 秒发一个心跳注释行：中间没有任何事件时，代理和浏览器会判定连接
    已死并断开，现场演示表现为「时间线走到一半就不动了」。
    """

    async def stream() -> AsyncIterator[str]:
        from app.db.session import connect

        sent: set[str] = set()
        idle = 0.0
        try:
            while True:
                con = connect()
                try:
                    rows = con.execute(
                        "SELECT step_id, seq, name, status, tool_name, started_at,"
                        " finished_at, error FROM task_step"
                        " WHERE task_id = ? ORDER BY seq",
                        (task_id,),
                    ).fetchall()
                    task = con.execute(
                        "SELECT status FROM task WHERE task_id = ?", (task_id,)
                    ).fetchone()
                finally:
                    con.close()

                fresh = [dict(r) for r in rows if r["step_id"] not in sent]
                for step in fresh:
                    sent.add(step["step_id"])
                    yield f"event: step\ndata: {json.dumps(step, ensure_ascii=False)}\n\n"
                idle = 0.0 if fresh else idle + poll_s

                if task is not None and task["status"] in (
                    "succeeded",
                    "failed",
                    "cancelled",
                ) and not fresh:
                    # 终态且没有新步骤：收尾。**只在确实没有新步骤时收**，
                    # 否则会漏掉最后完成的那一步。
                    yield f"event: done\ndata: {json.dumps({'status': task['status']})}\n\n"
                    return

                if idle >= 15.0:
                    yield ": keep-alive\n\n"
                    idle = 0.0

                await asyncio.sleep(poll_s)
        except asyncio.CancelledError:      # 客户端断开
            return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
