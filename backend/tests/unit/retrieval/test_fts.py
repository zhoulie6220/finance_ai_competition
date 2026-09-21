"""检索层测试：重点验证短查询不会静默丢失。"""

from __future__ import annotations

import sqlite3

import pytest

from app.db.session import connect_memory, init_schema
from app.retrieval.fts import MIN_FTS_CHARS, search_pages


@pytest.fixture()
def con() -> sqlite3.Connection:
    c = connect_memory()
    init_schema(c)
    c.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code, industry,"
        " fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
        ("p1", "测试", "某钢铁", "600019.SH", "steel", '["2024"]',
         "2026-09-21T00:00:00"),
    )
    for fid, period in (("f1", "2024"), ("f2", "2023")):
        c.execute(
            "INSERT INTO file (file_id, project_id, role, period, rel_path, sha256,"
            " bytes, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
            (fid, "p1", "annual_report", period, f"samples/{fid}.pdf", fid, 100,
             "2026-09-21T00:00:00"),
        )
    pages = [
        ("pg1", "f1", 86, "经营活动产生的现金流量净额 12,345 百万元"),
        ("pg2", "f1", 87, "存货周转天数较上年下降 3 天"),
        ("pg3", "f2", 74, "商誉减值损失计提 1,200 百万元"),
    ]
    for pid, fid, pno, text in pages:
        c.execute(
            "INSERT INTO document_page (page_id, file_id, page_no, text, text_source)"
            " VALUES (?,?,?,?,?)",
            (pid, fid, pno, text, "native"),
        )
    yield c
    c.close()


def test_long_query_uses_fts(con):
    hits = search_pages(con, "经营活动")
    assert [h.page_id for h in hits] == ["pg1"]
    assert hits[0].matched_by == "fts"


def test_two_char_query_falls_back_to_like(con):
    """两字中文词是本项目最常见的查询形态，绝不能返回空。"""
    assert MIN_FTS_CHARS == 3

    hits = search_pages(con, "存货")
    assert [h.page_id for h in hits] == ["pg2"]
    assert hits[0].matched_by == "like"
    assert "存货" in hits[0].snippet


def test_two_char_queries_for_core_financial_terms(con):
    for term, expected in (("商誉", "pg3"), ("存货", "pg2"), ("经营", "pg1")):
        hits = search_pages(con, term)
        assert [h.page_id for h in hits] == [expected], f"{term} 未命中"


def test_empty_query_returns_nothing(con):
    assert search_pages(con, "") == []
    assert search_pages(con, "   ") == []


def test_like_wildcards_are_escaped(con):
    """用户搜 '%' 不应该匹配到全部内容。"""
    assert search_pages(con, "%") == []
    assert search_pages(con, "_") == []


def test_file_filter(con):
    hits = search_pages(con, "百万元")
    assert {h.file_id for h in hits} == {"f1", "f2"}

    hits = search_pages(con, "百万元", file_ids=["f2"])
    assert {h.file_id for h in hits} == {"f2"}


def test_limit_is_respected(con):
    assert len(search_pages(con, "百万元", limit=1)) == 1
