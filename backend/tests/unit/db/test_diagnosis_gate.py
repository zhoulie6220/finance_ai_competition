"""诊断指数闸门的**反例**测试（会计口径 v1.1 §A.4）。

闸门不是文档里的一句话，是数据库层面的拒绝。要卡住两个方向：

  · 证据不足 → 不得出分（grade='insufficient_evidence' 要求 score IS NULL）
  · 出分     → 证据必须充足（H、C 各至少一条，R/P/Q 均可算且核验完成）
  · 证据不足 → 必须写明原因，否则页面只能显示一个光秃秃的「不出分」

只卡第一个方向是不够的：「grade='medium' 配 score=NULL」这种半截状态照样能
写进去——页面拿到它只会显示一片空白，没人分得清是「没跑」还是「跑坏了」。

刻意用 SQL 直接插，绕过引擎：这三层（引擎 / Pydantic / DB CHECK）里，
只有 DB 这一层是任何调用方都绕不过去的。引擎那层的测试在
tests/unit/engine/test_index.py。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db.session import connect_memory, init_schema

# 足以出分的一组完整证据：H 有 2 条、C 有 3 条、R/P/Q 都已核验完成
SCORABLE: dict[str, object] = {
    "observation_count": 10,
    "comparable_count": 7,
    "history_count": 2,
    "current_count": 3,
    "risk_ready": 1,
    "template_ready": 1,
    "quality_ready": 1,
    "coverage": 0.7,
    "score": "62.5",
    "grade": "medium",
    "conclusion_boundary": "本结果为研究辅助，不构成审计意见或证券买卖建议。",
    "insufficient_reason": None,
}

# 证据不足：Q 项的账龄明细库里没有数据源，适用项未核完
INSUFFICIENT: dict[str, object] = {
    **SCORABLE,
    "comparable_count": 2,
    "history_count": 0,
    "current_count": 2,
    "coverage": 0.2,
    "score": None,
    "grade": "insufficient_evidence",
    "insufficient_reason": "Q 项的账龄明细未披露，适用项未核完，按 v1.1 不得当作未触发。",
}


@pytest.fixture()
def con() -> sqlite3.Connection:
    c = connect_memory()
    init_schema(c)
    c.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code, industry,"
        " fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
        (
            "p1",
            "测试项目",
            "某钢铁",
            "600019.SH",
            "steel",
            '["2022","2023","2024"]',
            "2026-09-21T00:00:00",
        ),
    )
    return c


def _insert(con: sqlite3.Connection, **overrides: object) -> None:
    row = {**SCORABLE, **overrides}
    con.execute(
        "INSERT INTO diagnosis_run (run_id, project_id, rule_config_version,"
        " observation_count, comparable_count, history_count, current_count,"
        " risk_ready, template_ready, quality_ready, coverage, score, grade,"
        " conclusion_boundary, insufficient_reason, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["run_id"] if "run_id" in row else "r1",
            "p1",
            1,
            row["observation_count"],
            row["comparable_count"],
            row["history_count"],
            row["current_count"],
            row["risk_ready"],
            row["template_ready"],
            row["quality_ready"],
            row["coverage"],
            row["score"],
            row["grade"],
            row["conclusion_boundary"],
            row["insufficient_reason"],
            "2026-09-26T00:00:00",
        ),
    )


# ---------------------------------------------------------------- 正向

def test_complete_evidence_scores(con: sqlite3.Connection) -> None:
    """证据齐备时正常出分——反例测试也要有一条正例，否则「拦得太死」看不出来。"""
    _insert(con)
    assert con.execute("SELECT score FROM diagnosis_run").fetchone()[0] == "62.5"


def test_insufficient_evidence_stores_no_score(con: sqlite3.Connection) -> None:
    """证据不足时写入 NULL 分 + 原因，这是合法且唯一允许的形态。"""
    _insert(con, **{k: v for k, v in INSUFFICIENT.items() if k != "run_id"})
    row = con.execute(
        "SELECT score, grade, insufficient_reason FROM diagnosis_run"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "insufficient_evidence"
    assert row[2]


# ---------------------------------------------------------------- 反例

def test_insufficient_evidence_with_score_is_rejected(con: sqlite3.Connection) -> None:
    """★ 闸门不过却写了个分数——会在页面上变成一个没人复核的大号数字。"""
    with pytest.raises(sqlite3.IntegrityError):
        _insert(con, **{**INSUFFICIENT, "score": "50"})


def test_insufficient_evidence_without_reason_is_rejected(con: sqlite3.Connection) -> None:
    """不出分必须说明为什么，空字符串不算说明。"""
    with pytest.raises(sqlite3.IntegrityError):
        _insert(con, **{**INSUFFICIENT, "insufficient_reason": "   "})


def test_scoring_run_without_score_is_rejected(con: sqlite3.Connection) -> None:
    """grade 说能出分、score 却是 NULL —— 页面只会显示一片空白。"""
    with pytest.raises(sqlite3.IntegrityError):
        _insert(con, score=None)


@pytest.mark.parametrize("field", ["history_count", "current_count"])
def test_scoring_run_without_h_or_c_observation_is_rejected(
    con: sqlite3.Connection, field: str
) -> None:
    """闸门：H、C 各至少一条。

    全是本期主张（H 无观测）时只展示 C，不输出完整指数——否则「历史兑现度」
    那一项是被凭空当成满分的。
    """
    with pytest.raises(sqlite3.IntegrityError):
        _insert(con, **{field: 0})


@pytest.mark.parametrize("field", ["risk_ready", "template_ready", "quality_ready"])
def test_scoring_run_with_unverified_component_is_rejected(
    con: sqlite3.Connection, field: str
) -> None:
    """闸门：R、P、Q 均可算且核验完成。

    「适用项未核完」不得当作「未触发」——把未核验当成没风险，正是这道闸门
    要防的事。本数据缺账龄明细，所以 Q 项必然走到这里。
    """
    with pytest.raises(sqlite3.IntegrityError):
        _insert(con, **{field: 0})


def test_old_grade_name_is_rejected(con: sqlite3.Connection) -> None:
    """旧稿的 grade='insufficient' 已改名。

    留着旧值会让「闸门没过」的记录带着一个没人认识的分级躺进库里，
    页面只好自己猜怎么渲染。
    """
    with pytest.raises(sqlite3.IntegrityError):
        _insert(con, grade="insufficient")
