"""主张抽取落库的测试。

核心是**幂等**：抽取重跑一次就会往表里再插一份，n 和 N 同时翻倍、
指数整体偏移——**而且不报错**。防重复靠的是确定性 claim_id，
不是唯一索引（v1.1 允许同一句拆成多条主张，加唯一索引会误杀）。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db.session import connect_memory, init_schema, load_seeds
from app.skills.narrative import claim_stats, extract_and_store, list_claims

NOW = "2026-09-26T00:00:00+00:00"

SECTION_TEXT = """
三、公司关于公司未来发展的讨论与分析
2024年公司计划实现钢材销量增长5%以上，持续推进降本增效，深挖内部潜力。
报告期内公司销售商品坯材4,976.3万吨，销量同比上升183万吨，回款情况明显改善。
公司力争提升高端产品占比，优化产品结构。
冷轧碳钢板卷        41,655    35,695    14.31    -
"""


@pytest.fixture()
def con() -> sqlite3.Connection:
    c = connect_memory()
    init_schema(c)
    load_seeds(c)
    c.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code, industry,"
        " fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
        ("p1", "测试", "某钢铁", "600019.SH", "steel",
         '["2023","2024"]', NOW),
    )
    c.execute(
        "INSERT INTO file (file_id, project_id, role, period, rel_path, sha256,"
        " bytes, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        ("f1", "p1", "annual_report", "2023", "samples/x.pdf", "abc", 10, NOW),
    )
    c.execute(
        "INSERT INTO mdna_section (section_id, file_id, heading, kind, page_from,"
        " page_to, text) VALUES (?,?,?,?,?,?,?)",
        ("s1", "f1", "三、未来发展的讨论与分析", "outlook", 12, 13, SECTION_TEXT),
    )
    c.commit()
    return c


# ---------------------------------------------------------------- 幂等


def test_extraction_is_idempotent(con: sqlite3.Connection) -> None:
    """★ 重跑不产生重复主张。

    重跑一次表里就多一份，n 和 N 同时翻倍、指数整体偏移，**且不报错**。
    确定性 claim_id 是防这个的唯一可靠手段。
    """
    first = extract_and_store(con, "p1", now=NOW)
    assert first.claims_inserted > 0

    second = extract_and_store(con, "p1", now=NOW)
    assert second.claims_inserted == 0
    assert second.claims_skipped_existing == first.claims_inserted

    assert claim_stats(con, "p1")["total"] == first.claims_inserted


def test_claim_id_is_deterministic(con: sqlite3.Connection) -> None:
    extract_and_store(con, "p1", now=NOW)
    first_ids = {r["claim_id"] for r in list_claims(con, "p1")}
    con.execute("DELETE FROM claim WHERE project_id = 'p1'")
    con.commit()
    extract_and_store(con, "p1", now=NOW)
    assert {r["claim_id"] for r in list_claims(con, "p1")} == first_ids


# ---------------------------------------------------------------- 内容


def test_claims_keep_the_original_sentence(con: sqlite3.Connection) -> None:
    """claim_text 必须是原句、一字不改——证据链的终点就是它在年报里的位置。"""
    extract_and_store(con, "p1", now=NOW)
    for row in list_claims(con, "p1"):
        assert row["claim_text"] in SECTION_TEXT


def test_table_rows_never_become_claims(con: sqlite3.Connection) -> None:
    """★ 表格行不能变成主张。

    不剔除的话 claim 表会被数字垃圾填满，N 虚高、判定全是胡话——
    「41,655 35,695 14.31」这样的「主张」判定出来的冲突没有任何意义。
    """
    extract_and_store(con, "p1", now=NOW)
    for row in list_claims(con, "p1"):
        assert "41,655" not in row["claim_text"]


def test_unverifiable_claims_are_background_only(con: sqlite3.Connection) -> None:
    """不可验证的主张必须标 background_only，**不得进入评分**。

    这是 v1.1 与 docs/01 都写明的硬约束。产品结构升级这一类的
    主判据字段字典里没有，所以抽出来的都是不可验证的。
    """
    extract_and_store(con, "p1", now=NOW)
    for row in list_claims(con, "p1"):
        if not row["verifiable"]:
            assert row["background_only"] == 1
            assert row["status"] == "needs_review"


def test_forbidden_simplifications_ride_along(con: sqlite3.Connection) -> None:
    """每条主张都带上该主题「禁止的简化推断」，供人工复核时对照。

    这些是会计口径 v1.1 §A.6 点名的判据边界——不随主张一起给出来，
    复核的人就只会看到一个判定结论，看不到它可能踩的那条线。
    """
    extract_and_store(con, "p1", now=NOW)
    rows = list_claims(con, "p1")
    assert rows
    cost = [r for r in rows if r["claim_type"] == "cost"]
    if cost:
        assert any("毛利率" in f for f in cost[0]["forbidden_simplifications"])


def test_forward_claim_targets_next_year(con: sqlite3.Connection) -> None:
    """2023 年报里说「2024 年…」，目标期间是 2024。

    搞反了会产生事后偏见的「处处支持」，而且不报错。
    """
    extract_and_store(con, "p1", now=NOW)
    forward = [r for r in list_claims(con, "p1") if r["period_norm"] == "2024"]
    assert forward, "没有解析出任何指向 2024 的前瞻主张"
    assert any(r["period_expr"] and "2024" in r["period_expr"] for r in forward)


def test_empty_project_reports_why(con: sqlite3.Connection) -> None:
    """空库要说明原因，不能安静地返回 0 条。

    「没抽出主张」和「库是空的」在页面上长得一样，但处置完全不同。
    """
    summary = extract_and_store(con, "no-such-project", now=NOW)
    assert summary.claims_inserted == 0
    assert summary.warnings
    assert "数据包" in summary.warnings[0]


def test_summary_reports_what_it_skipped(con: sqlite3.Connection) -> None:
    """摘要要如实报告跳过了什么，不只报成功的数。"""
    summary = extract_and_store(con, "p1", now=NOW)
    assert summary.sections_scanned == 1
    assert summary.sentences_scanned > 0
    assert summary.unverifiable >= 0
    assert "不可验证" in summary.describe()
