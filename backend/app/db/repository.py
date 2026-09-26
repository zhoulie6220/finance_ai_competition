"""取数层：把库里的行读成接口要用的形状。

两条规矩贯穿全文：

1. **金额一律以字符串出入，绝不经过 float。** 库里的 `value_millions` 本来就是
   TEXT 存的 Decimal 字符串；一旦在中途 `float()` 一次，`12,345,678,901.23`
   往返就成了 `12345678901.229998`。JSON number 是 IEEE 754 双精度，这就是
   契约要求「金额序列化为字符串」的原因。

2. **只暴露 `validated` 且可比的行的**那几个接口**（指数、估值、网格），
   一律走 `v_fact_verified` 或 `v_fact_grid_company` 视图**——让不可信数据在
   SQL 层就进不来，而不是靠每个调用方记得过滤。

连接由调用方持有（FastAPI 的依赖注入），本模块自己不 `connect()`——
这样同一个请求里的多次查询共用一条连接，也便于测试注入内存库。
"""

from __future__ import annotations

import sqlite3
from typing import Any

# ---------------------------------------------------------------- 项目


def list_projects(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """项目列表，附带各自的数据量，供首页选择器用。"""
    rows = con.execute(
        """
        SELECT p.project_id, p.name, p.company_name, p.stock_code, p.industry,
               p.base_scope, p.fiscal_years,
               (SELECT COUNT(*) FROM file f WHERE f.project_id = p.project_id) AS file_count,
               (SELECT COUNT(*) FROM financial_fact x
                 WHERE x.project_id = p.project_id AND x.status = 'validated') AS fact_count,
               (SELECT COUNT(*) FROM mdna_section m
                 JOIN file f2 ON f2.file_id = m.file_id
                WHERE f2.project_id = p.project_id) AS mdna_count
        FROM project p
        ORDER BY p.company_name
        """
    ).fetchall()
    return [_project_row(r) for r in rows]


def get_project(con: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = con.execute(
        "SELECT project_id, name, company_name, stock_code, industry,"
        " base_scope, fiscal_years FROM project WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    out = _project_row(row)
    out["files"] = list_files(con, project_id)
    return out


def _project_row(row: sqlite3.Row) -> dict[str, Any]:
    import json

    try:
        years = json.loads(row["fiscal_years"])
    except (ValueError, TypeError):
        # fiscal_years 是 JSON 数组。解析不出来时给空列表——**不猜**。
        # 但要把话说出来，否则页面会显示一个空网格而不说明为什么。
        years = []
    out: dict[str, Any] = {
        "project_id": row["project_id"],
        "name": row["name"],
        "company_name": row["company_name"],
        "stock_code": row["stock_code"],
        "industry": row["industry"],
        "base_scope": row["base_scope"],
        "fiscal_years": years,
    }
    for key in ("file_count", "fact_count", "mdna_count"):
        if key in row.keys():
            out[key] = row[key]
    return out


def list_files(con: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT file_id, role, period, rel_path, page_count, parse_status"
        " FROM file WHERE project_id = ? ORDER BY period, role",
        (project_id,),
    ).fetchall()
    return [
        {
            "file_id": r["file_id"],
            "role": r["role"],
            "period": r["period"],
            "file_name": r["rel_path"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
            "page_count": r["page_count"],
            "parse_status": r["parse_status"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 指标网格


def fact_grid(
    con: sqlite3.Connection,
    project_id: str,
    *,
    scope: str | None = None,
    company_id: str | None = None,
) -> dict[str, Any]:
    """「指标 × 年度」网格。数据源是 `v_fact_grid_company`。

    ⚠ **不要换成 `v_fact_grid`。** 那个视图硬编码了 `is_primary = 1`，
    而华菱、首钢的全部事实都是 `is_primary = 0`（那是主公司标记，不是
    「有没有数据」标记）。换过去之后这两个项目的网格会每格都是 `not_found`，
    页面上看起来像「年报没披露」，而且不报任何错。实测：旧视图 0 格有值，
    新视图 138 / 139 格有值。

    返回结构按**指标分行、年度分列**，前端直接渲染，不做任何业务计算。
    """
    project = get_project(con, project_id)
    if project is None:
        return {}
    scope = scope or project["base_scope"]

    # ⚠ **不要在这里加 `AND scope = ?`。**
    # 视图的 `scope` 列取自 `f.scope`，而 LEFT JOIN 没匹配到事实时它是 NULL——
    # 拿它做过滤会把**所有空格子一起滤掉**，网格只剩「有数据的指标」，
    # 于是「没找到」和「这个指标不存在」在页面上长得一模一样，而且不报错。
    # 口径已经在视图的 JOIN 条件（f.scope = p.base_scope）里限定过了。
    #
    # ⚠ 同理，视图目前只服务 base_scope。传了别的口径就明确拒绝，
    # 不能悄悄按 base_scope 返回——那会让调用方以为拿到了母公司口径的数。
    if scope != project["base_scope"]:
        raise ValueError(
            f"网格视图目前只支持项目的基础口径 {project['base_scope']!r}，"
            f"收到 {scope!r}。母公司口径尚未在解析阶段采集。"
        )

    params: list[Any] = [project_id]
    company_clause = ""
    if company_id:
        company_clause = " AND company_id = ?"
        params.append(company_id)

    rows = con.execute(
        f"""
        SELECT metric_key, label_cn, statement, unit_kind, period, fact_id,
               value_millions, unit, raw_unit, status, comparable,
               incomparable_reason, source_file, source_page, confidence
        FROM v_fact_grid_company
        WHERE project_id = ?{company_clause}
        ORDER BY statement, metric_key, period
        """,
        params,
    ).fetchall()

    metrics: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = metrics.get(r["metric_key"])
        if entry is None:
            entry = {
                "metric_key": r["metric_key"],
                "label_cn": r["label_cn"],
                "statement": r["statement"],
                "unit_kind": r["unit_kind"],
                "cells": {},
            }
            metrics[r["metric_key"]] = entry
        entry["cells"][r["period"]] = {
            "fact_id": r["fact_id"],
            # 金额以字符串出网，绝不经过 float
            "value": r["value_millions"],
            "unit": r["unit"],
            "raw_unit": r["raw_unit"],
            "status": r["status"],
            "comparable": bool(r["comparable"]),
            "incomparable_reason": r["incomparable_reason"],
            "source_file": r["source_file"],
            "source_page": r["source_page"],
            "confidence": r["confidence"],
        }

    return {
        "project_id": project_id,
        "company_name": project["company_name"],
        "scope": scope,
        "periods": project["fiscal_years"],
        "metrics": list(metrics.values()),
    }


# ---------------------------------------------------------------- 单笔事实与原文


def get_fact(con: sqlite3.Connection, fact_id: str) -> dict[str, Any] | None:
    """一笔事实的完整契约字段。证据抽屉的主体。"""
    row = con.execute(
        """
        SELECT f.*, fl.rel_path AS file_name, fl.period AS file_period
        FROM financial_fact f
        LEFT JOIN file fl ON fl.file_id = f.source_file_id
        WHERE f.fact_id = ?
        """,
        (fact_id,),
    ).fetchone()
    if row is None:
        return None
    keys = row.keys()
    return {
        "fact_id": row["fact_id"],
        "project_id": row["project_id"],
        "company_id": row["company_id"],
        "is_primary": bool(row["is_primary"]),
        "metric": row["metric_key"],
        # 十个契约字段
        "value": row["value_millions"],
        "unit": row["unit"],
        "period": row["period"],
        "scope": row["scope"],
        "source_file": row["source_file"],
        "source_page": row["source_page"],
        "source_text": row["source_text"],
        "confidence": row["confidence"],
        "status": row["status"],
        # 可追溯字段
        "period_kind": row["period_kind"],
        "value_raw": row["value_raw"],
        "raw_unit": row["raw_unit"],
        "unit_factor": row["unit_factor"],
        "source_table": row["source_table"],
        "source_row_label": row["source_row_label"],
        "source_printed_page": row["source_printed_page"],
        "bbox": row["bbox"],
        "extractor": row["extractor"],
        "comparable": bool(row["comparable"]),
        "incomparable_reason": row["incomparable_reason"],
        "restated": bool(row["restated"]),
        "file_name": row["file_name"],
        "file_period": row["file_period"],
        # v1.1 新增的符号依据；旧数据为 NULL 是正常的
        "sign_basis": row["sign_basis"] if "sign_basis" in keys else None,
        "mapped_from": row["mapped_from"] if "mapped_from" in keys else None,
    }


def get_page(con: sqlite3.Connection, page_id: str) -> dict[str, Any] | None:
    """一页正文，附带前后页编号，供「点回原文」翻页。"""
    row = con.execute(
        """
        SELECT p.page_id, p.file_id, p.page_no, p.printed_page_no, p.text,
               p.text_source, p.has_table,
               f.rel_path AS file_name, f.period, f.role
        FROM document_page p
        JOIN file f ON f.file_id = p.file_id
        WHERE p.page_id = ?
        """,
        (page_id,),
    ).fetchone()
    if row is None:
        return None

    neighbors = con.execute(
        "SELECT MIN(page_no), MAX(page_no) FROM document_page WHERE file_id = ?",
        (row["file_id"],),
    ).fetchone()

    return {
        "page_id": row["page_id"],
        "file_id": row["file_id"],
        "file_name": _basename(row["file_name"]),
        "period": row["period"],
        "role": row["role"],
        "page_no": row["page_no"],
        "printed_page_no": row["printed_page_no"],
        "text": row["text"],
        "text_source": row["text_source"],
        "has_table": bool(row["has_table"]),
        "first_page_no": neighbors[0],
        "last_page_no": neighbors[1],
    }


def find_page_for_fact(con: sqlite3.Connection, fact_id: str) -> dict[str, Any] | None:
    """由一笔事实定位到它所在的页。

    用 `(file_id, page_no)` 定位而不是用 `source_page` 直接当 `page_id`——
    前者是物理页序，后者是主键，两者不是一回事。
    """
    row = con.execute(
        "SELECT source_file_id, source_page FROM financial_fact WHERE fact_id = ?",
        (fact_id,),
    ).fetchone()
    if row is None:
        return None
    page = con.execute(
        "SELECT page_id FROM document_page WHERE file_id = ? AND page_no = ?",
        (row["source_file_id"], row["source_page"]),
    ).fetchone()
    if page is None:
        return None
    return get_page(con, page["page_id"])


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------- 规则参数


def list_rule_config(con: sqlite3.Connection, industry: str = "") -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT key, value, value_type, industry, label_cn, description,"
        " unit, default_value, min_value, max_value"
        " FROM rule_config WHERE industry IN ('', ?) ORDER BY key",
        (industry,),
    ).fetchall()
    return [dict(r) for r in rows]


def current_rule_config_version(con: sqlite3.Connection) -> int:
    row = con.execute(
        "SELECT COALESCE(MAX(version), 1) FROM rule_config_version"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 1


# ---------------------------------------------------------------- 概览


def db_counts(con: sqlite3.Connection) -> dict[str, int]:
    """库里的关键计数。`/api/health` 用它判断「库是不是空的」。

    空库是能打开的——不做这个检查的话，一个刚重建完还没导数据的库
    会让所有页面显示空白，而没有任何地方提示原因。
    """
    out: dict[str, int] = {}
    for label, sql in (
        ("project", "SELECT COUNT(*) FROM project"),
        ("file", "SELECT COUNT(*) FROM file"),
        ("document_page", "SELECT COUNT(*) FROM document_page"),
        ("financial_fact", "SELECT COUNT(*) FROM financial_fact"),
        ("mdna_section", "SELECT COUNT(*) FROM mdna_section"),
        ("claim", "SELECT COUNT(*) FROM claim"),
        ("claim_match", "SELECT COUNT(*) FROM claim_match"),
        ("diagnosis_run", "SELECT COUNT(*) FROM diagnosis_run"),
        ("fact_check_result", "SELECT COUNT(*) FROM fact_check_result"),
        ("llm_call", "SELECT COUNT(*) FROM llm_call"),
    ):
        try:
            out[label] = con.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            out[label] = -1        # 表不存在：库结构不对，比 0 更该被注意
    return out
