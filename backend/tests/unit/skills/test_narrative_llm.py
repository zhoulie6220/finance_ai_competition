"""LLM 版主张抽取的测试。

全部用 `FakeTransport`——**不联网、不花钱**。这条链路的逻辑（表格剔除、
期间归一化、id 去重、原句回原文核对）值得单独测，而它跟「能不能连上
DeepSeek」是两回事。

## 这里的每一条都对应一个实测踩到的坑

三个都是**在真实语料上跑才暴露**的：

1. 模型把财务摘要表的行当成主张（39 条里 30 条是表格行）
2. 期间没归一化，所有主张都落 needs_review
3. id 里没带主题，同一句话拆成两条时后者被静默丢掉（40 条丢 14 条）
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.agents.llm.client import LlmClient
from app.agents.llm.prompts.registry import PromptRegistry
from app.agents.llm.settings import LlmSettings
from app.agents.llm.transport import FakeTransport
from app.db.session import connect_memory, init_schema, load_seeds
from app.skills.narrative_llm import extract_claims_llm

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "app" / "agents" / "llm" / "prompts"

#: 一段真实形态的 MD&A：既有正文，也有财务摘要表的行。
SECTION = """2015年，钢铁行业形势严峻。2015年国内粗钢产量8.04亿吨，同比下降2.3%。
公司加大降本控费挖潜力度，全年实现营业总收入1641.2亿元。
营业成本  149,258  168,931  -11.65
财务费用  2,393  488  390.57
"""


def _reply(claims: list[dict]) -> str:
    return json.dumps({"claims": claims}, ensure_ascii=False)


def make_client(replies: list[str], con: sqlite3.Connection) -> LlmClient:
    return LlmClient(
        settings=LlmSettings(model="fake", api_key="sk-test", seed=42),
        transport=FakeTransport(replies=replies),
        prompts=PromptRegistry(PROMPTS_DIR),
        sink=lambda record: None,
        clock=lambda: "2026-09-27T00:00:00+00:00",
    )


@pytest.fixture()
def con() -> sqlite3.Connection:
    c = connect_memory()
    init_schema(c)
    load_seeds(c)
    c.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code, industry,"
        " fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
        ("p1", "测试", "某钢铁", "600019.SH", "steel", '["2015"]',
         "2026-09-27T00:00:00"),
    )
    c.execute(
        "INSERT INTO file (file_id, project_id, role, period, rel_path, sha256,"
        " bytes, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        ("f1", "p1", "annual_report", "2015", "samples/x.pdf", "abc", 10,
         "2026-09-27T00:00:00"),
    )
    c.execute(
        "INSERT INTO mdna_section (section_id, file_id, heading, kind, page_from,"
        " page_to, text) VALUES (?,?,?,?,?,?,?)",
        ("s1", "f1", "经营情况讨论与分析", "business_review", 10, 10, SECTION),
    )
    c.execute(
        "INSERT INTO document_page (page_id, file_id, page_no, text, text_source)"
        " VALUES (?,?,?,?,?)",
        ("pg10", "f1", 10, SECTION, "native"),
    )
    c.commit()
    return c


# ---------------------------------------------------------------- 表格行


def test_table_rows_from_the_model_are_dropped(con: sqlite3.Connection) -> None:
    """★ **模型会把财务报表的行当成主张抽出来。**

    实测：39 条返回里有 30 条是这种——

        营业成本  149,258  168,931  -11.65
        财务费用  2,393  488  390.57

    它们不是管理层说的话，是报表里的数字行。放进 claim 表会让 N 虚高、
    判定全是胡话，而且**看起来完全合理**——「成本下降了 11.65%」
    读起来就像一条真主张。

    规则层的 `looks_like_table_row` 早就解决了这件事。这一层直接用它，
    不重写——这正是「叠在规则法线上」的字面意思。
    """
    con_reply = _reply([
        {"text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
         "claim_type": "demand", "direction": "down", "verifiable": True,
         "confidence": 0.9},
        {"text": "营业成本  149,258  168,931  -11.65",
         "claim_type": "cost", "direction": "down", "verifiable": True,
         "confidence": 0.9},
        {"text": "财务费用  2,393  488  390.57",
         "claim_type": "cost", "direction": "up", "verifiable": True,
         "confidence": 0.9},
    ])
    s = extract_claims_llm(
        con, "p1", client=make_client([con_reply], con), now="2026-09-27T00:00:00+00:00"
    )
    assert s.claims_returned == 3
    assert s.table_rows == 2
    assert s.claims_inserted == 1
    assert "表格行" in s.describe()

    texts = [r["claim_text"] for r in con.execute("SELECT claim_text FROM claim")]
    assert texts == ["2015年国内粗钢产量8.04亿吨，同比下降2.3%。"]


# ---------------------------------------------------------------- 期间


def test_period_is_normalised_by_the_rule_baseline(con: sqlite3.Connection) -> None:
    """★ 期间归一化**复用规则法基线**，不让模型自己给。

    模型给的是 `period_expr`（原文表述「2015年」），
    归一化用的还是规则层那个 `resolve_period`。

    第一版没接这一步，结果**所有主张都落 needs_review**——
    因为 `verifiable` 要求 `period_norm` 非空。

    两边各写一份归一化逻辑的话，口径会慢慢分叉，而分叉不会报错，
    只会让「同一句话在两种抽取法下指向不同年度」。
    """
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "demand", "direction": "down",
        "period_expr": "2015年", "verifiable": True, "confidence": 0.9,
    }])
    extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    row = con.execute(
        "SELECT period_norm, period_expr, status FROM claim"
    ).fetchone()
    assert row["period_norm"] == "2015"
    assert row["period_expr"] == "2015年"
    assert row["status"] == "validated"


# ---------------------------------------------------------------- id 去重


def test_same_sentence_with_two_themes_is_two_claims(con: sqlite3.Connection) -> None:
    """★ 同一句话拆成两条不同主题时，**不能被自己的 id 去重掉**。

    v1.1 明确允许这种拆分：「一句话涉及多个指标的，拆成多条」。

    第一版的 id 只按 (章节, 原文) 算，40 条里 14 条被
    `INSERT OR IGNORE` 静默丢掉——而丢掉这件事**不会报错**，
    只是主张数莫名少了一截。
    """
    line = "公司加大降本控费挖潜力度，全年实现营业总收入1641.2亿元。"
    reply = _reply([
        {"text": line, "claim_type": "cost", "direction": "improve",
         "metric_key": "operating_cost", "verifiable": True, "confidence": 0.8},
        {"text": line, "claim_type": "other", "direction": "up",
         "metric_key": "revenue", "verifiable": True, "confidence": 0.8},
    ])
    s = extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    assert s.claims_inserted == 2, "同一句话的两条主张被 id 去重掉了"


def test_rerun_is_idempotent(con: sqlite3.Connection) -> None:
    """重跑不产生重复——靠确定性 id，不是唯一索引。"""
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "demand", "direction": "down", "verifiable": True,
        "confidence": 0.9,
    }])
    client = make_client([reply, reply], con)
    first = extract_claims_llm(con, "p1", client=client, now="2026-09-27T00:00:00+00:00")
    second = extract_claims_llm(con, "p1", client=client, now="2026-09-27T00:00:00+00:00")
    assert first.claims_inserted == 1
    assert second.claims_inserted == 0
    assert second.claims_skipped_existing == 1


def test_ids_are_distinct_from_the_rule_extractor(con: sqlite3.Connection) -> None:
    """LLM 版的 id 前缀是 `cl-llm-`，规则法是 `cl-`。

    两者**并存**是刻意的：验收标准要求「能和规则法对照」，
    用同一套 id 的话后写的会覆盖先写的，就对照不起来了。
    """
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "demand", "direction": "down", "verifiable": True,
        "confidence": 0.9,
    }])
    extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    cid = con.execute("SELECT claim_id FROM claim").fetchone()[0]
    assert cid.startswith("cl-llm-")


# ---------------------------------------------------------------- 原句核对


def test_sentence_not_in_source_is_flagged(con: sqlite3.Connection) -> None:
    """★ 模型编的句子必须被抓住。

    `claim_text` 是证据链的终点——它必须能在年报里被找到。
    所以每一条都回原文核一遍（去掉空白后比对），找不到的落 `needs_review`
    且置信度归零，**不静默收下**。

    这是数字守卫在文本层面的对应物：数字守卫拦编造的**数字**，
    这一道拦编造的**句子**。
    """
    reply = _reply([{
        "text": "公司预计2026年实现净利润翻倍。",      # 原文里没有这句
        "claim_type": "other", "direction": "up", "verifiable": True,
        "confidence": 0.95,
    }])
    s = extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    assert s.not_in_source == 1
    row = con.execute("SELECT status, confidence, verifiable FROM claim").fetchone()
    assert row["status"] == "needs_review"
    assert row["confidence"] == 0
    assert row["verifiable"] == 0


def test_whitespace_differences_do_not_break_the_check(con: sqlite3.Connection) -> None:
    """模型复述原文时常把多空格规范化掉——那不是编造。

    「全年实现营业总收入1641.2亿元」在原文里可能是
    「全年实现营业总收入  1641.2  亿元」，去空白后应能匹配上。
    """
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "demand", "direction": "down", "verifiable": True,
        "confidence": 0.9,
    }])
    s = extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    assert s.not_in_source == 0


# ---------------------------------------------------------------- 取值兜底


def test_unknown_claim_type_falls_back_to_other(con: sqlite3.Connection) -> None:
    """模型给了个没见过的主题 → 归一成 `other`，**不丢这条**。

    丢掉的话，模型偶尔造一个新词就会静默少一条主张，
    而少掉的那条没人会去找。而归一成 other 至少内容还在。
    """
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "降本增效",       # 模型自造的中文主题
        "direction": "down", "verifiable": True, "confidence": 0.9,
    }])
    s = extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    assert s.claims_inserted == 1
    assert con.execute("SELECT claim_type FROM claim").fetchone()[0] == "other"


def test_unknown_direction_falls_back_to_unknown(con: sqlite3.Connection) -> None:
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "demand", "direction": "大幅下降", "verifiable": True,
        "confidence": 0.9,
    }])
    extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    assert con.execute("SELECT direction FROM claim").fetchone()[0] == "unknown"


def test_confidence_is_clamped(con: sqlite3.Connection) -> None:
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "demand", "direction": "down", "verifiable": True,
        "confidence": 3.7,          # 模型给超范围的值
    }])
    extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    conf = con.execute("SELECT confidence FROM claim").fetchone()[0]
    assert 0.0 <= conf <= 1.0


def test_empty_claims_is_not_an_error(con: sqlite3.Connection) -> None:
    """抽不到任何主张是完全正常的输出，不是失败。

    把它当失败的话，一段没有可验证主张的正文会触发 schema_repair 重试，
    白白多花一次调用的钱，最后还是抽不到。
    """
    s = extract_claims_llm(
        con, "p1", client=make_client([_reply([])], con),
        now="2026-09-27T00:00:00+00:00",
    )
    assert s.sections_attempted == 1
    assert s.sections_failed == 0
    assert s.claims_inserted == 0


# ---------------------------------------------------------------- 主判据


def test_claims_get_a_primary_indicator(con: sqlite3.Connection) -> None:
    """★ 抽出来的主张**必须带 `claim_indicator`**，否则不参与判定。

    第一版漏了这一步：770 条主张一条都没被判定，全落进「无主判据」，
    而**不报任何错**。页面上看起来只是「模型抽了很多但不能判」，
    实际是接线断了。

    匹配读的是 `claim_indicator`，读不到就跳过——所以这一步不是可选的。
    """
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "demand", "direction": "down", "verifiable": True,
        "confidence": 0.9, "metric_key": "steel_sales_volume",
    }])
    extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    row = con.execute(
        "SELECT ci.metric_key, ci.role, ci.matched_by FROM claim_indicator ci"
        " JOIN claim c ON c.claim_id = ci.claim_id"
    ).fetchone()
    assert row is not None, "主张没有主判据——它不会参与任何判定"
    assert row["metric_key"] == "steel_sales_volume"
    assert row["role"] == "primary"


def test_indicator_falls_back_to_the_theme_table(con: sqlite3.Connection) -> None:
    """模型没给主判据时，**退回主题表的主判据**——复用规则层那一份映射。

    不写这条兜底的话，模型少给一个字段，那条主张就静默不参与判定了。
    """
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "demand", "direction": "down", "verifiable": True,
        "confidence": 0.9, "metric_key": None,      # 模型没给
    }])
    extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    metric = con.execute("SELECT metric_key FROM claim_indicator").fetchone()[0]
    assert metric == "steel_sales_volume"       # 需求与产销类的主判据


def test_unknown_metric_falls_back_instead_of_failing(con: sqlite3.Connection) -> None:
    """★ 模型给了个字典里没有的键——**退回主题表，不要让整批写库失败**。

    `claim_indicator.metric_key` 有外键指向 `metric_definition`，
    模型自造一个键名会违反外键——而拒绝发生在**整批写入的中途**，
    前面写进去的回滚、后面的全没写。
    """
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "demand", "direction": "down", "verifiable": True,
        "confidence": 0.9, "metric_key": "made_up_metric_key",
    }])
    s = extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    assert s.claims_inserted == 1
    metric = con.execute("SELECT metric_key FROM claim_indicator").fetchone()[0]
    assert metric == "steel_sales_volume"


def test_theme_without_a_metric_is_counted_not_dropped(con: sqlite3.Connection) -> None:
    """主题表和模型都给不出主判据时，**如实计数**，不静默丢弃。

    `other`/`macro`/`risk` 这几类在规则层的主题表里没有主判据，
    所以它们不参与判定——但这件事要**看得见**，
    否则「模型抽了很多却不能判」会被当成「模型抽得不准」。
    """
    reply = _reply([{
        "text": "2015年国内粗钢产量8.04亿吨，同比下降2.3%。",
        "claim_type": "macro", "direction": "down", "verifiable": True,
        "confidence": 0.9, "metric_key": None,
    }])
    s = extract_claims_llm(
        con, "p1", client=make_client([reply], con), now="2026-09-27T00:00:00+00:00"
    )
    assert s.no_metric == 1
    assert "无主判据" in s.describe()
