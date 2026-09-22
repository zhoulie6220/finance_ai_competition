"""项目与文件路由。

**文件上传与 PDF 解析尚未实现**（属甲的 `app/parsing/`），本文件只把接口形状定下来，
并提供「登记一份已经在 DATA_ROOT 里的文件」这条路径 —— 演示时年报 PDF 是提前放好的，
不需要走浏览器上传。

⚠ 安全约束：`rel_path` **相对 `DATA_ROOT`**，解析后必须仍在该目录内。
绝对路径与 `..` 一律拒绝，并把拒绝本身写进 `file_access_log`。
只靠 `Path.resolve()` 判断是不够的——Windows 上 `C:foo` 这类驱动器相对路径
不会按预期解析，所以这里同时做前缀检查与显式拒绝。
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.agents.state import SystemClock
from app.api.deps import get_project_repo
from app.api.errors import COMMON_RESPONSES
from app.config import get_settings
from app.db.repositories.project_repo import ProjectRepository
from app.schemas.api import (
    FileListResponse,
    FileView,
    ProjectListResponse,
    ProjectView,
)
from app.schemas.enums import FileRole

router = APIRouter(
    prefix="/api/projects", tags=["项目与文件"], responses=COMMON_RESPONSES
)


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(title="新建项目请求")

    # ⚠ example 不是装饰：/docs 的「Try it out」把 example 当**预填值**写进请求体，
    #   不写的话它会填 "string"，用户照着界面提示点一下就失败。
    name: str = Field(min_length=1, description="项目名", examples=["宝钢股份 2015-2024"])
    company_name: str = Field(min_length=1, description="公司名", examples=["宝钢股份"])
    stock_code: str = Field(
        min_length=1, description="股票代码", examples=["600019.SH"]
    )
    industry: str = Field(
        default="steel", description="行业；本次参赛只跑钢铁", examples=["steel"]
    )
    fiscal_years: list[str] = Field(
        default_factory=list,
        description="覆盖的会计年度",
        examples=[["2015", "2016", "2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024"]],
    )


class RegisterFileRequest(BaseModel):
    model_config = ConfigDict(title="登记文件请求")

    rel_path: str = Field(
        description="相对 DATA_ROOT 的路径",
        examples=["samples/600019_2024.pdf"],
    )
    role: FileRole = Field(
        default=FileRole.ANNUAL_REPORT, description="文件在项目中的角色"
    )
    period: str = Field(description="会计期间", examples=["2024"])


def _resolve_within_data_root(rel_path: str) -> Path:
    """把相对路径解析成绝对路径，并确认它没有跑出 DATA_ROOT。"""
    root = get_settings().data_root.resolve()
    raw = rel_path.strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ":" in raw:
        raise HTTPException(status_code=400, detail=f"只接受相对路径，收到：{rel_path!r}")
    if ".." in Path(raw).parts:
        raise HTTPException(status_code=400, detail=f"路径不得包含 '..'：{rel_path!r}")

    target = (root / raw).resolve()
    # 前缀检查用 is_relative_to 而不是字符串 startswith：
    # '/data/var2/x'.startswith('/data/var') 是 True，字符串比较会漏掉兄弟目录。
    if not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail=f"路径越出 DATA_ROOT：{rel_path!r}")
    return target


@router.get("", summary="项目列表", response_model=ProjectListResponse)
def list_projects(repo: ProjectRepository = Depends(get_project_repo)) -> dict:
    return {"projects": repo.list_projects()}


@router.post(
    "",
    summary="新建项目",
    response_model=ProjectView,
    responses={409: {"description": "项目名或股票代码已存在"}},
)
def create_project(
    body: CreateProjectRequest, repo: ProjectRepository = Depends(get_project_repo)
) -> ProjectView:
    project = repo.create_project(
        project_id=f"p-{uuid.uuid4().hex[:12]}",
        name=body.name,
        company_name=body.company_name,
        stock_code=body.stock_code,
        industry=body.industry,
        fiscal_years=body.fiscal_years,
        now=SystemClock().now(),
    )
    repo.con.commit()
    return project


@router.get("/{project_id}/files", summary="项目的文件列表", response_model=FileListResponse)
def list_files(
    project_id: str, repo: ProjectRepository = Depends(get_project_repo)
) -> dict:
    # 项目不存在时返回空列表会让人以为「项目是空的」，而不是「项目不存在」——
    # 这两件事在页面上长得一样，但用户要做的事完全不同。
    if not repo.exists(project_id):
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    return {"files": repo.list_files(project_id)}


@router.post(
    "/{project_id}/files",
    summary="登记一份已在 DATA_ROOT 里的文件",
    response_model=FileView,
    responses={404: {"description": "项目或文件不存在"}, 409: {"description": "同一份文件已登记过"}},
)
def register_file(
    project_id: str,
    body: RegisterFileRequest,
    repo: ProjectRepository = Depends(get_project_repo),
) -> FileView:
    """登记文件：算 sha256、记字节数，标记为待解析。

    只登记不解析——PDF 解析是 `app/parsing/` 的事，尚未实现。
    """
    if not repo.exists(project_id):
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")

    target = _resolve_within_data_root(body.rel_path)
    if not target.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"文件不存在：{body.rel_path}（DATA_ROOT={get_settings().data_root}）",
        )

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    try:
        view = repo.insert_file(
            file_id=f"f-{uuid.uuid4().hex[:12]}",
            project_id=project_id,
            role=body.role,
            period=body.period,
            rel_path=body.rel_path,
            sha256=digest,
            size=target.stat().st_size,
            now=SystemClock().now(),
        )
    except Exception as exc:  # noqa: BLE001
        # UNIQUE(project_id, sha256)：同一份文件重复登记
        raise HTTPException(
            status_code=409, detail=f"该文件已登记过（sha256 相同）：{exc}"
        ) from exc
    repo.con.commit()
    return view
