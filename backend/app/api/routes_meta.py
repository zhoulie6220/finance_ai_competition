"""元信息路由：健康检查、工具清单、Skill 清单。

这三个接口是**给评审看的**：`/api/tools` 直接回答「你们的 Tool 模块长什么样」，
`/api/skills` 回答「Skill 有哪些」。把它们做成 REST 而不是只存在于代码里，
是因为演示现场打开一个 URL 就能看到，比翻目录树有说服力。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_app_settings
from app.api.errors import COMMON_RESPONSES
from app.config import Settings
from app.db.session import BACKEND_DIR
from app.schemas.api import (
    HealthResponse,
    MetaResponse,
    SkillListResponse,
    ToolListResponse,
)

router = APIRouter(tags=["元信息"], responses=COMMON_RESPONSES)


@router.get(
    "/health",
    summary="健康检查",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "数据库不可用"}},
)
def health(settings: Settings = Depends(get_app_settings)) -> Any:
    """健康检查。

    刻意**不**在数据库不可用时返回 200：一个永远返回 200 的健康检查
    等于没有健康检查，前端会拿着一个连不上库的后端继续跑，直到某个页面
    报出一句莫名其妙的错。
    """
    db_ok = True
    db_detail: str | None = None
    counts: dict[str, int] = {}
    try:
        from app.db.session import connect

        con = connect()
        try:
            for table in ("metric_definition", "rule_config"):
                counts[table] = con.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            counts["tables"] = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts_%'"
            ).fetchone()[0]
        finally:
            con.close()
    except (sqlite3.Error, RuntimeError) as exc:
        db_ok, db_detail = False, str(exc)

    from app.db.session import default_db_path

    payload: dict[str, Any] = {
        "status": "ok" if db_ok else "degraded",
        "database": {"ok": db_ok, "path": str(default_db_path()), "counts": counts},
        "offline_mode": settings.offline_mode,
        "llm_configured": bool(settings.llm_api_key),
        "model": settings.llm_model,
    }
    if db_detail:
        payload["database"]["error"] = db_detail
    if db_ok is False:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get("/api/tools", summary="已登记的 Tool 清单", response_model=ToolListResponse)
def list_tools() -> dict[str, Any]:
    """工具登记表。

    `deterministic=True` 的是纯计算，结果可复算；`False` 的含 LLM 调用。
    前端据此把「程序算的」与「模型理解的」分开呈现。
    """
    from app.tools.registry import REGISTRY

    return {
        "count": len(REGISTRY.names()),
        "tools": [
            {
                "name": s.name,
                "version": s.version,
                "description": s.description_cn,
                "deterministic": s.deterministic,
                "side_effects": s.side_effects,
                "transport": s.transport.value,
                "input_schema": s.input_schema,
                "output_schema": s.output_schema,
            }
            for s in REGISTRY.all_specs()
        ],
        # MCP 侧直接复用同一份 input_schema，这里预览一下 MCP 会暴露成什么样
        "mcp_preview": REGISTRY.mcp_tools(read_only_only=True),
    }


@router.get("/api/skills", summary="已登记的 Skill 清单", response_model=SkillListResponse)
def list_skills() -> dict[str, Any]:
    from app.skills import REGISTRY  # 导入即注册

    return {
        "count": len(REGISTRY.all()),
        "skills": [
            {"key": s.key, "name": s.name_cn, "description": s.description_cn}
            for s in REGISTRY.all()
        ],
    }


@router.get("/api/meta", summary="运行环境", response_model=MetaResponse)
def meta(settings: Settings = Depends(get_app_settings)) -> dict[str, Any]:
    return {
        "backend_dir": str(BACKEND_DIR),
        "data_root": str(settings.data_root),
        "settings": settings.redacted(),
    }
