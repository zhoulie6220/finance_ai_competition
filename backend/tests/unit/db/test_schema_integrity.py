"""数据库完整性回归测试。

这里每一条断言都对应一个**曾经真实存在、且不会报错只会静默损坏数据**的问题。
它们必须在每次改动 schema 后继续通过。

已覆盖的历史缺陷：
  1. PRAGMA foreign_keys 是每连接生效的，写在 schema.sql 里只能作用一次，
     应用开的每条新连接外键都是关闭的 —— 全部外键约束静默失效。
  2. page_fts 没有任何同步触发器，写入页面后全文索引始终为空，检索静默返回 0 条。
  3. period_kind 含 'prior' 会让同一笔经济事实按两种 kind 各存一行，聚合时重复计算。
  4. 同一笔事实可以被多条观测同时标记为「已采纳」，冲突要到很后面才暴露。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from app.db.session import connect_memory, init_schema

D = Decimal


@pytest.fixture()
def con() -> sqlite3.Connection:
    c = connect_memory()
    init_schema(c)
    c.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code, industry,"
        " fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
        ("p1", "测试", "某钢铁", "600019.SH", "steel",
         '["2022","2023","2024"]', "2026-09-21T00:00:00"),
    )
    c.execute(
        "INSERT INTO file (file_id, project_id, role, period, rel_path, sha256, bytes,"
        " uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        ("f1", "p1", "annual_report", "2024", "samples/a.pdf", "abc", 100,
         "2026-09-21T00:00:00"),
    )
    c.execute(
        "INSERT INTO metric_definition (metric_key, label_cn, aliases, statement,"
        " value_type, unit_kind, sign_convention, display_order)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("revenue", "营业收入", '["营业收入"]', "income", "flow", "currency",
         "positive_is_good", 10),
    )
    yield c
    c.close()


FACT_COLS = (
    "fact_id,project_id,company_id,is_primary,metric_key,value_millions,unit,period,"
    "period_kind,scope,source_file,source_file_id,source_page,source_text,confidence,"
    "status,extractor,created_at"
)


def insert_fact(con, fact_id="k1", **over):
    vals = dict(
        fact_id=fact_id, project_id="p1", company_id="c1", is_primary=1,
        metric_key="revenue", value_millions="100", unit="百万元", period="2024",
        period_kind="current", scope="consolidated", source_file="2024年年度报告.pdf",
        source_file_id="f1", source_page=86, source_text="营业收入 100",
        confidence=0.96, status="validated", extractor="rule:v1",
        created_at="2026-09-21T00:00:00",
    )
    vals.update(over)
    cols = ",".join(vals)
    con.execute(
        f"INSERT INTO financial_fact ({cols}) VALUES ({','.join('?' * len(vals))})",
        list(vals.values()),
    )


# ---------------------------------------------------------------- 1. 外键必须真的生效


def test_foreign_keys_are_on_for_every_connection():
    con = connect_memory()
    try:
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        con.close()


def test_fact_with_nonexistent_file_is_rejected(con):
    """这是最要命的一类静默损坏：事实表指向一个根本不存在的来源文件。"""
    with pytest.raises(sqlite3.IntegrityError):
        insert_fact(con, source_file_id="不存在的文件")


def test_fact_with_unknown_metric_is_rejected(con):
    with pytest.raises(sqlite3.IntegrityError):
        insert_fact(con, metric_key="不存在的指标")


def test_observation_with_nonexistent_file_is_rejected(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO fact_observation (observation_id, project_id, company_id,"
            " metric_key, period, period_kind, scope, value_raw, source_file_id,"
            " source_page, source_location, source_text, confidence, extractor,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("o1", "p1", "c1", "revenue", "2024", "current", "consolidated", "100",
             "不存在的文件", 86, "main_statement", "营业收入 100", 0.9, "rule:v1",
             "2026-09-21T00:00:00"),
        )


# ---------------------------------------------------------------- 2. FTS 同步


def test_fts_is_populated_on_insert(con):
    """没有触发器时这里会静默返回 0 条——检索失效但不报错。"""
    con.execute(
        "INSERT INTO document_page (page_id, file_id, page_no, printed_page_no, text,"
        " text_source) VALUES (?,?,?,?,?,?)",
        ("pg1", "f1", 86, "86", "经营活动产生的现金流量净额 12,345 百万元", "native"),
    )
    hits = con.execute(
        "SELECT page_id FROM page_fts WHERE page_fts MATCH ?", ("经营活动",)
    ).fetchall()
    assert [h[0] for h in hits] == ["pg1"]


def test_fts_follows_update_and_delete(con):
    # 查询串一律 ≥3 字：trigram 无法匹配更短的串（见 test_trigram_..._two_chars）
    con.execute(
        "INSERT INTO document_page (page_id, file_id, page_no, text, text_source)"
        " VALUES (?,?,?,?,?)",
        ("pg1", "f1", 86, "应收账款周转天数", "native"),
    )
    assert con.execute("SELECT COUNT(*) FROM page_fts WHERE page_fts MATCH ?",
                       ("应收账款",)).fetchone()[0] == 1

    con.execute("UPDATE document_page SET text = ? WHERE page_id = ?",
                ("存货周转天数", "pg1"))
    assert con.execute("SELECT COUNT(*) FROM page_fts WHERE page_fts MATCH ?",
                       ("应收账款",)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM page_fts WHERE page_fts MATCH ?",
                       ("存货周转",)).fetchone()[0] == 1

    con.execute("DELETE FROM document_page WHERE page_id = ?", ("pg1",))
    assert con.execute("SELECT COUNT(*) FROM page_fts WHERE page_fts MATCH ?",
                       ("存货周转",)).fetchone()[0] == 0


def test_trigram_tokenizer_silently_fails_on_two_chars(con):
    """记录 trigram 的查询长度下限。

    这不是我们希望的行为，而是分词器的固有限制。写下来是为了：任何直接查
    page_fts 的代码，遇到两字查询都会静默拿到空结果，必须有测试钉住这个事实。
    真正的检索入口是 app.retrieval.fts.search_pages()，它会回退到 LIKE。
    """
    con.execute(
        "INSERT INTO document_page (page_id, file_id, page_no, text, text_source)"
        " VALUES (?,?,?,?,?)",
        ("pg1", "f1", 86, "存货周转天数", "native"),
    )
    two = con.execute("SELECT COUNT(*) FROM page_fts WHERE page_fts MATCH ?",
                      ("存货",)).fetchone()[0]
    three = con.execute("SELECT COUNT(*) FROM page_fts WHERE page_fts MATCH ?",
                        ("存货周",)).fetchone()[0]
    assert two == 0, "若此项开始为 1，说明 SQLite 放宽了 trigram 限制，可简化检索层"
    assert three == 1


def test_fts_index_never_drifts_from_source(con):
    """索引行数必须始终等于页面行数，否则说明触发器有漏。"""
    for i in range(5):
        con.execute(
            "INSERT INTO document_page (page_id, file_id, page_no, text, text_source)"
            " VALUES (?,?,?,?,?)",
            (f"pg{i}", "f1", i + 1, f"第{i}页 资产负债率", "native"),
        )
    con.execute("DELETE FROM document_page WHERE page_no = 3")
    con.execute("UPDATE document_page SET text = '现金流量表' WHERE page_no = 2")

    pages = con.execute("SELECT COUNT(*) FROM document_page").fetchone()[0]
    indexed = con.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0]
    assert pages == indexed


# ---------------------------------------------------------------- 3. period_kind


@pytest.mark.parametrize("kind", ["current", "instant", "opening", "average"])
def test_accepted_period_kinds(con, kind):
    insert_fact(con, fact_id=f"ok_{kind}", period_kind=kind)


def test_period_kind_rejects_prior(con):
    """'prior' 会让 FY2023 在 2023 年报和 2024 年报里各存一行，聚合重复计算。"""
    with pytest.raises(sqlite3.IntegrityError):
        insert_fact(con, period_kind="prior")


def test_same_fact_from_two_reports_collapses_to_one_row(con):
    """同一笔事实无论取自哪份年报，都必须是同一行。"""
    insert_fact(con, fact_id="a", period="2023", source_file="2023年年度报告.pdf")
    with pytest.raises(sqlite3.IntegrityError):
        insert_fact(con, fact_id="b", period="2023", source_file="2024年年度报告.pdf")


# ---------------------------------------------------------------- 4. 观测采纳唯一性


def obs(con, oid, *, resolution="pending", fact_id=None, note=None, value="100"):
    con.execute(
        "INSERT INTO fact_observation (observation_id, project_id, company_id,"
        " metric_key, period, period_kind, scope, value_raw, source_file_id,"
        " source_page, source_location, source_text, confidence, extractor,"
        " created_at, resolution, resolved_fact_id, rejection_note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, "p1", "c1", "revenue", "2024", "current", "consolidated", value,
         "f1", 86, "main_statement", "营业收入 100", 0.9, "rule:v1",
         "2026-09-21T00:00:00", resolution, fact_id, note),
    )


def test_only_one_observation_can_be_adopted(con):
    insert_fact(con)
    obs(con, "o1", resolution="adopted", fact_id="k1")
    with pytest.raises(sqlite3.IntegrityError):
        obs(con, "o2", resolution="adopted", fact_id="k1")


def test_rejected_observation_requires_reason(con):
    with pytest.raises(sqlite3.IntegrityError):
        obs(con, "o1", resolution="rejected")
    obs(con, "o2", resolution="rejected", note="精度低于主表，未采纳")


def test_pending_observation_cannot_bind_a_fact(con):
    insert_fact(con)
    with pytest.raises(sqlite3.IntegrityError):
        obs(con, "o1", resolution="pending", fact_id="k1")


# ---------------------------------------------------------------- 三条硬规则（回归）


def test_validated_fact_requires_full_source(con):
    insert_fact(con)  # 合法的基线
    for over in (
        {"source_text": "   "},
        {"source_page": 0},
        {"value_millions": None},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            insert_fact(con, fact_id=f"bad_{list(over)[0]}", **over)


def test_incomparable_requires_reason(con):
    with pytest.raises(sqlite3.IntegrityError):
        insert_fact(con, comparable=0, incomparable_reason=None)


def test_missing_period_is_absent_not_zero(con):
    """缺失即无行：视图中显示 not_found 且值为 NULL，绝不插补 0。"""
    insert_fact(con, period="2024")
    row = con.execute(
        "SELECT status, value_millions FROM v_fact_grid"
        " WHERE metric_key='revenue' AND period='2022'"
    ).fetchone()
    assert row["status"] == "not_found"
    assert row["value_millions"] is None


def test_verified_view_hides_unvalidated(con):
    insert_fact(con, fact_id="ok")
    insert_fact(con, fact_id="nr", period="2023", status="needs_review")
    ids = [r[0] for r in con.execute("SELECT fact_id FROM v_fact_verified")]
    assert ids == ["ok"]
