"""项目与文件的读写。

**JSON 列在这一层转，不往上传。** `project.fiscal_years` 在库里是 JSON 文本
（`'["2022","2023"]'`），原样塞进响应的话，前端拿到的是字符串而不是数组——
`years.map(...)` 直接抛错，或者更糟：`for (const y of years)` 会逐个字符迭代。
这条约定与 `task_repo` 相同，理由也相同。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.schemas.api import FileView, ProjectView
from app.schemas.enums import FileRole, ParseStatus, Scope


def _loads(raw: str | None, default: Any) -> Any:
    """JSON 文本 → Python 对象。坏 JSON 退回默认值，不抛。"""
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


class ProjectRepository:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    # ------------------------------------------------------------------ project

    @staticmethod
    def _to_project(row: sqlite3.Row) -> ProjectView:
        return ProjectView(
            project_id=row["project_id"],
            name=row["name"],
            company_name=row["company_name"],
            stock_code=row["stock_code"],
            industry=row["industry"],
            base_currency=row["base_currency"],
            # fiscal_years 是 JSON 文本；不转的话前端拿到的是 '["2022","2023"]'
            fiscal_years=_loads(row["fiscal_years"], []),
            base_scope=Scope(row["base_scope"]).value,
            status=row["status"],
            created_at=row["created_at"],
        )

    def list_projects(self) -> list[ProjectView]:
        rows = self.con.execute(
            "SELECT * FROM project ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
        return [self._to_project(r) for r in rows]

    def get_project(self, project_id: str) -> ProjectView:
        row = self.con.execute(
            "SELECT * FROM project WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"项目不存在：{project_id}")
        return self._to_project(row)

    def create_project(
        self,
        *,
        project_id: str,
        name: str,
        company_name: str,
        stock_code: str,
        industry: str,
        fiscal_years: list[str],
        now: str,
    ) -> ProjectView:
        self.con.execute(
            "INSERT INTO project (project_id, name, company_name, stock_code,"
            " industry, fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                project_id, name, company_name, stock_code, industry,
                json.dumps(fiscal_years, ensure_ascii=False), now,
            ),
        )
        return self.get_project(project_id)

    def exists(self, project_id: str) -> bool:
        """项目是否存在。写入之前先问一句，好过让外键抛 sqlite 原文。"""
        return self.con.execute(
            "SELECT 1 FROM project WHERE project_id=?", (project_id,)
        ).fetchone() is not None

    # --------------------------------------------------------------------- file

    @staticmethod
    def _to_file(row: sqlite3.Row) -> FileView:
        return FileView(
            file_id=row["file_id"],
            project_id=row["project_id"],
            role=FileRole(row["role"]),
            period=row["period"],
            rel_path=row["rel_path"],
            sha256=row["sha256"],
            bytes=row["bytes"],
            page_count=row["page_count"],
            # 库里是 0/1；原样返回的话前端写 `if (f.is_scanned)` 会恒为真——
            # 字符串 '0' 在 JS 里是真值。这个坑不报错，只是每份文件都被当成扫描件。
            is_scanned=bool(row["is_scanned"]),
            parse_status=ParseStatus(row["parse_status"]),
            parse_error=row["parse_error"],
            uploaded_at=row["uploaded_at"],
        )

    def list_files(self, project_id: str) -> list[FileView]:
        rows = self.con.execute(
            "SELECT * FROM file WHERE project_id=? ORDER BY period, rowid",
            (project_id,),
        ).fetchall()
        return [self._to_file(r) for r in rows]

    def insert_file(
        self,
        *,
        file_id: str,
        project_id: str,
        role: FileRole,
        period: str,
        rel_path: str,
        sha256: str,
        size: int,
        now: str,
    ) -> FileView:
        """登记一份文件。

        `parse_status` 停在 'pending' 是**诚实的状态**：解析是 `app/parsing/` 的事，
        还没实现。登记完就标成 'parsed' 的话，页面会把没解析过的文件显示成
        「已解析但没数据」，而这两件事对用户的意思完全不同。
        """
        self.con.execute(
            "INSERT INTO file (file_id, project_id, role, period, rel_path, sha256,"
            " bytes, parse_status, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                file_id, project_id, role.value, period, rel_path, sha256,
                size, ParseStatus.PENDING.value, now,
            ),
        )
        row = self.con.execute(
            "SELECT * FROM file WHERE file_id=?", (file_id,)
        ).fetchone()
        return self._to_file(row)
