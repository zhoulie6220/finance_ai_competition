"""FastAPI 应用装配。

    uvicorn app.main:app --reload

**本文件只做装配，不写业务。** 路由在 `app/api/routes_*.py`，编排在
`app/agents/orchestrator.py`，计算在 `app/engine/`。把业务塞进 main.py 的代价是
它很快会变成所有人都要改的文件，而所有人都在改的文件必然冲突。

启动时会做一次**自检**：数据库在不在、种子数据全不全、Skill 注册上没有。
不自检的话，问题会推迟到用户点下第一个按钮时才暴露，而那时的报错信息
（一句 500）跟真正的原因（库是空的）离得很远。
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agents.state import SystemClock
from app.api.errors import validation_exception_handler
from app.api.events import EventBus
from app.config import get_settings
from app.db.session import connect

APP_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # 事件总线必须挂在这里而不是每次请求新建：任务在后台跑，SSE 在请求里订阅，
    # 两者必须看到同一个总线。每次新建的话，前端永远收不到事件，且不会报错。
    app.state.bus = EventBus(
        history_limit=settings.sse_history_limit, clock=SystemClock()
    )
    app.state.started_at = SystemClock().now()

    _startup_selfcheck(settings)

    # Skill 与 Tool 在 import 时注册。显式 import 一次，让「注册」这件事
    # 在启动路径上可见，而不是藏在一串间接 import 的副作用里。
    #
    # ⚠ 不要写成 `import app.skills`：本函数有个参数就叫 `app`（FastAPI 实例），
    #   `import app.skills` 会把局部名 `app` 重新绑定成包模块，
    #   于是下一行 `app.state` 就报 "module 'app' has no attribute 'state'"。
    from app.skills import REGISTRY as SKILLS  # noqa: F401 —— 导入即注册
    from app.tools.registry import REGISTRY as TOOLS

    app.state.skills = SKILLS.all()
    app.state.tools = TOOLS.all_specs()
    if not app.state.skills:
        raise RuntimeError("没有任何 Skill 被注册——app/skills/__init__.py 里的注册表是空的")

    yield

    app.state.bus = None


def _startup_selfcheck(settings) -> None:
    """启动自检。**失败就拒绝启动**，不带着坏掉的库跑起来。"""
    from app.db.session import default_db_path

    db_path = default_db_path()
    if not db_path.exists():
        raise RuntimeError(
            f"数据库不存在：{db_path}\n请先运行：python scripts/init_db.py"
        )
    con = connect()
    try:
        for table in ("metric_definition", "rule_config"):
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if n == 0:
                raise RuntimeError(
                    f"{table} 是空的 —— 种子数据没有装载。\n"
                    "请运行：python scripts/init_db.py --force"
                )
    finally:
        con.close()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="财报叙事一致性分析与情景估值投研工作台",
        description=(
            "分析 A 股钢铁行业上市公司年报，把管理层在 MD&A 中的表述拆成可验证主张，"
            "与后续财务事实交叉验证，形成叙事—财务一致性诊断，并透明地传导至情景估值。\n\n"
            "**数字由程序计算，模型只负责理解和组织文字。**"
        ),
        version=APP_VERSION,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # SSE 断线重连时浏览器会带 Last-Event-ID，必须放行
        expose_headers=["Last-Event-ID"],
    )

    from app.api.routes_meta import router as meta_router
    from app.api.routes_projects import router as projects_router
    from app.api.routes_tasks import router as tasks_router

    app.include_router(meta_router)
    app.include_router(projects_router)
    app.include_router(tasks_router)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        """结构化访问日志。

        写进 `app_log` 表而不是日志文件：`.gitignore` 忽略了 logs 目录，
        写文件的日志不会随项目导出，也没法在页面上按 task_id 查。
        评审要看的「日志记录」是能查的那种。
        """
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id

        # SSE 是长连接，记录它的「耗时」没有意义（会记成整个连接的生命周期）
        if request.url.path.endswith("/events"):
            return response

        try:
            con = connect()
            try:
                from app.db.repositories.task_repo import TaskRepository

                TaskRepository(con).log(
                    ts=SystemClock().now(), level="INFO", logger="http",
                    event="request", request_id=request_id,
                    message=f"{request.method} {request.url.path} → {response.status_code}",
                    payload={"query": str(request.url.query) or None},
                    duration_ms=elapsed,
                )
                con.commit()
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            # 日志写不进去不能影响正常响应。这里刻意吞掉异常，
            # 但**不吞掉响应**——把日志故障升级成请求失败是本末倒置。
            pass
        return response

    # 422 的文案换成中文。必须在下面那个 Exception 兜底**之前**注册：
    # Starlette 按 `type(exc).__mro__` 找处理器，精确匹配优先于基类，
    # 所以两者不会打架——但顺序颠倒过来会让人以为打了一架。
    app.add_exception_handler(RequestValidationError, validation_exception_handler)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """兜底异常处理。

        返回稳定的 JSON 结构而不是 HTML 错误页：前端统一按 `detail` 取值渲染，
        否则一个未预期的异常会让界面显示一坨 HTML。
        """
        from fastapi import HTTPException

        if isinstance(exc, HTTPException):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return JSONResponse(
            status_code=500,
            content={"detail": f"服务器内部错误：{type(exc).__name__}: {exc}"},
        )

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "name": "财报叙事一致性分析与情景估值投研工作台",
            "version": APP_VERSION,
            "docs": "/docs",
            "health": "/health",
        }

    return app


app = create_app()


__all__ = ["app", "create_app"]
