"""REST 路由。

**薄层**：只做参数校验与调用，不写业务逻辑。所有算术在 `app/engine/`，
所有取数在 `app/db/repository.py`。这条边界的意义在赛事要求里写得很直白——
「计算可复算、过程可追溯」，而路由层里藏一个 `round(x, 2)` 就没人能复算了。

响应里的金额一律是**字符串**（见 repository.py 的说明）。前端只做格式化，
不做任何业务计算。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.agents.llm.settings import LlmSettings
from app.db import repository
from app.skills import checks as checks_skill

router = APIRouter(prefix="/api")


def get_con() -> Any:
    """每请求一条连接。**必须经由 `connect()`**——见 app/db/session.py。"""
    from app.db.session import connect

    con = connect()
    try:
        yield con
    finally:
        con.close()


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_project(con: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    project = repository.get_project(con, project_id)
    if project is None:
        # 报出 id 而不是只说「不存在」——三个项目的 id 长得像，报出来好查
        raise HTTPException(status_code=404, detail=f"没有这个项目：{project_id}")
    return project


# ---------------------------------------------------------------- 健康检查


@router.get("/health")
def health(con: sqlite3.Connection = Depends(get_con)) -> dict[str, Any]:
    """启动时调一次。

    返回值里 `llm_mode` 驱动前端的「离线回放模式」横幅——**这是诚信要求**，
    不是装饰：docs/00 把「回放时装作实时」列为诚信问题。

    `counts` 里的 0 要当回事：空库是能打开的，不做这个检查的话，
    刚重建完还没导数据的库会让所有页面显示空白，而没有任何地方提示原因。
    """
    settings = LlmSettings.from_env()
    counts = repository.db_counts(con)
    db_ok = counts.get("project", 0) > 0 and counts.get("financial_fact", 0) > 0
    return {
        "status": "ok" if db_ok else "empty",
        "db_ok": db_ok,
        "db_hint": (
            None
            if db_ok
            else "库里没有数据。重建之后需要跑 python scripts/merge_data_pack.py 导回数据包。"
        ),
        "counts": counts,
        "llm_mode": settings.mode,
        "llm_configured": settings.configured,
        "llm_description": settings.describe(),
        "offline_mode": settings.offline_mode,
        "rule_config_version": repository.current_rule_config_version(con),
    }


# ---------------------------------------------------------------- 项目


@router.get("/projects")
def list_projects(con: sqlite3.Connection = Depends(get_con)) -> list[dict[str, Any]]:
    return repository.list_projects(con)


@router.get("/projects/{project_id}")
def get_project(
    project_id: str, con: sqlite3.Connection = Depends(get_con)
) -> dict[str, Any]:
    return _require_project(con, project_id)


@router.get("/projects/{project_id}/fact-grid")
def fact_grid(
    project_id: str,
    scope: str | None = Query(default=None, description="默认取项目的 base_scope"),
    company_id: str | None = Query(default=None),
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    """「指标 × 年度」网格。

    读的是 `v_fact_grid_company` 而不是 `v_fact_grid`——后者硬编码
    `is_primary = 1`，会让华菱、首钢的网格每格都是 not_found 且不报错。
    """
    _require_project(con, project_id)
    try:
        grid = repository.fact_grid(
            con, project_id, scope=scope, company_id=company_id
        )
    except ValueError as exc:
        # 不支持的口径：明确拒绝，不悄悄按别的口径返回
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not grid:
        raise HTTPException(status_code=404, detail=f"没有这个项目：{project_id}")
    return grid


# ---------------------------------------------------------------- 事实与原文


@router.get("/facts/{fact_id}")
def get_fact(fact_id: str, con: sqlite3.Connection = Depends(get_con)) -> dict[str, Any]:
    fact = repository.get_fact(con, fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail=f"没有这笔事实：{fact_id}")
    return fact


@router.get("/facts/{fact_id}/page")
def fact_page(
    fact_id: str, con: sqlite3.Connection = Depends(get_con)
) -> dict[str, Any]:
    """由一笔事实跳回年报原文。证据链的终点。

    返回的是**已抽取的正文**而不是 PDF 切片：样例 PDF 有 94MB，被 .gitignore
    挡在仓库外，评委 clone 下来根本没有那些文件。而抽出来的正文在库里、
    能全文检索、能高亮，比 PDF 更好用。
    """
    page = repository.find_page_for_fact(con, fact_id)
    if page is None:
        raise HTTPException(
            status_code=404,
            detail=f"这笔事实（{fact_id}）定位不到原文页。多半是 source_page 与"
            f" document_page.page_no 对不上——检查解析环节的页码绑定。",
        )
    return page


@router.get("/pages/{page_id}")
def get_page(page_id: str, con: sqlite3.Connection = Depends(get_con)) -> dict[str, Any]:
    page = repository.get_page(con, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"没有这一页：{page_id}")
    return page


# ---------------------------------------------------------------- 勾稽校验


@router.get("/checks")
def run_checks(
    project_id: str = Query(..., description="项目 id"),
    persist: bool = Query(default=True, description="是否把结论写回 fact_check_result"),
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    """跑三表勾稽校验。

    每次调用都**重算**而不是读上次的结果：数据可能变了，读一份过期结论
    比重新算一遍危险得多——后者慢，前者错。

    `persist=0` 时只算不落库，用于「改完规则参数先看看会怎样」。
    """
    _require_project(con, project_id)
    if persist:
        report, load, _ = checks_skill.run_and_persist(con, project_id, now=_utc_now())
    else:
        report, load = checks_skill.run_project_checks(con, project_id)
    return checks_skill.report_to_json(report, load)


@router.get("/checks/stored")
def stored_checks(
    project_id: str = Query(...), con: sqlite3.Connection = Depends(get_con)
) -> dict[str, Any]:
    """读回上次落库的结果，供「与上次运行对照」。"""
    _require_project(con, project_id)
    return {"project_id": project_id, "results": checks_skill.load_results(con, project_id)}


# ---------------------------------------------------------------- 规则参数


@router.get("/rule-config")
def rule_config(
    industry: str = Query(default=""), con: sqlite3.Connection = Depends(get_con)
) -> list[dict[str, Any]]:
    """口径参数。页面提供查看/修改/恢复默认，改的就是这些行。

    引擎从库里读这些值而不是用代码里的默认值——否则改完参数之后，
    页面显示的口径和实际生效的口径会对不上，而且不报错。
    """
    return repository.list_rule_config(con, industry)
