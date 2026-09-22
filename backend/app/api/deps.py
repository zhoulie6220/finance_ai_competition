"""FastAPI 依赖。

两件与「可复现」直接相关的事在这里定死：

1. **连接一律走 `app.db.session.connect()`**，绝不 `sqlite3.connect()`。
   外键是每连接生效的，直接连会得到一条外键关闭的连接，全部外键约束静默失效。
2. **`EventBus` 是进程级单例。** SSE 订阅发生在 HTTP 请求里，而任务在后台跑——
   两者必须看到同一个总线。每次请求新建一个，前端就永远收不到任何事件，
   而且不会报错，只是时间线一直空着。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request

from app.agents.orchestrator import Orchestrator
from app.agents.state import Clock, SystemClock
from app.api.events import EventBus
from app.config import Settings, get_settings
from app.db.repositories.project_repo import ProjectRepository
from app.db.repositories.task_repo import TaskRepository
from app.db.session import connect


def get_clock() -> Clock:
    """时钟。测试里覆盖这个依赖就能把整条时间线钉死。"""
    return SystemClock()


def get_bus(request: Request) -> EventBus:
    """进程级事件总线。放在 `app.state` 上，与 ASGI 应用同生命周期。"""
    return request.app.state.bus


def get_app_settings() -> Settings:
    return get_settings()


def db_conn() -> Iterator[sqlite3.Connection]:
    """每个请求一条连接。

    不开连接池：本项目数据量约 900 行事实，SQLite 开一条连接是微秒级的事，
    而连接池会引入「同一条连接被两个协程交错使用」的风险——SQLite 连接不是
    协程安全的，交错使用的后果是事务边界错乱，且不会立刻报错。
    """
    con = connect()
    try:
        yield con
    finally:
        con.close()


def get_repo(request: Request) -> Iterator[TaskRepository]:
    con = connect()
    try:
        yield TaskRepository(con)
    finally:
        con.close()


def get_project_repo() -> Iterator[ProjectRepository]:
    con = connect()
    try:
        yield ProjectRepository(con)
    finally:
        con.close()


def build_orchestrator(repo: TaskRepository, bus: EventBus, clock: Clock) -> Orchestrator:
    """装配一个编排器。

    每次调用都现装：编排器持有一条连接，跨请求复用就等于跨请求复用连接。
    """
    from app.skills.base import REGISTRY as SKILLS

    return Orchestrator(
        repo=repo,
        bus=bus,
        clock=clock,
        skills=SKILLS,
        offline=get_settings().offline_mode,
    )
