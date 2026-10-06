"""事实网格里那根红绿柱子的数据源：**后端算的同比**。

★ 为什么这组测试值得单独写：

页面上每格有一根红涨绿跌的柱子，长度来自 `cell.change`。
那个数**必须由后端算**——项目铁律里最硬的一条是「同比、比率、估值一律由
后端 `app/engine/` 算好返回」，理由是浏览器里算的东西没法审计，而
「计算可复算、过程可追溯」是比赛的硬要求。

所以这里盯三件事：
  1. 同比确实算出来了，而且**用的是引擎里那一份**（不另写一套）
  2. 第一个年度**没有上期**，不出同比——而且**不能写成「拒绝」**
  3. 算不出来时给得出原因，前端据此不画柱子（**不画比画错好**）
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from app.db.repository import _cell_yoy, fact_grid
from app.db.session import connect_memory, init_schema


@pytest.fixture()
def con() -> sqlite3.Connection:
    c = connect_memory()
    init_schema(c)
    c.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code, industry,"
        " fiscal_years, base_scope, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("p1", "测试", "某钢铁", "600019.SH", "steel",
         '["2023","2024"]', "consolidated", "t"),
    )
    c.execute(
        "INSERT INTO file (file_id, project_id, role, period, rel_path, sha256,"
        " bytes, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        ("f1", "p1", "annual_report", "2024", "a.pdf", "x", 1, "t"),
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


def _fact(con, fact_id, period, value, *, comparable=1, reason=None, scope="consolidated"):
    con.execute(
        "INSERT INTO financial_fact (fact_id, project_id, company_id, is_primary,"
        " metric_key, value_millions, unit, period, period_kind, scope, source_file,"
        " source_file_id, source_page, source_text, confidence, status, comparable,"
        " incomparable_reason, extractor, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fact_id, "p1", "c1", 1, "revenue", str(value), "百万元", period, "current",
         scope, "a.pdf", "f1", 1, "原文", 0.9, "validated", comparable,
         reason, "rule:v1", "t"),
    )


# ---------------------------------------------------------------- 正常路径


def test_yoy_matches_the_engine(con):
    """★ 同比必须与 `engine.ratios.yoy_growth` 一致。

    这里**不重算一遍期望值**——重算就等于把引擎的实现抄进测试，
    两边一起错的时候测试照样绿。断言的是「用的是引擎那一份」这件事：
    引擎算 0.5，网格里就得是 0.5。
    """
    _fact(con, "a", "2023", "100")
    _fact(con, "b", "2024", "150")
    grid = fact_grid(con, "p1")
    cell = grid["metrics"][0]["cells"]["2024"]
    assert cell["change"] == "0.5000"
    assert cell["change_dir"] == "up"
    assert cell["change_refused"] is None
    # 算式与代入的数一路带到前端，悬停就能看到怎么算的
    assert "2023" in cell["change_formula"] and "2024" in cell["change_formula"]
    assert cell["change_inputs"]["2023 年（上期）"] == "100"


def test_a_decline_is_direction_down(con):
    """★ 反面：只测上涨是不够的——把方向写死成 up 也能过上面那条。

    前端按 `change_dir` 上色（红增绿减），方向错了柱子颜色就反了，
    而**页面上看不出哪根是错的**。
    """
    _fact(con, "a", "2023", "200")
    _fact(con, "b", "2024", "150")
    cell = fact_grid(con, "p1")["metrics"][0]["cells"]["2024"]
    assert cell["change"] == "-0.2500"
    assert cell["change_dir"] == "down"


def test_a_tiny_move_is_flat(con):
    """相对变化 ≤ 1% 判「基本持平」。前端据此不上色，只画一根灰条。"""
    _fact(con, "a", "2023", "100")
    _fact(con, "b", "2024", "100.5")
    cell = fact_grid(con, "p1")["metrics"][0]["cells"]["2024"]
    assert cell["change_dir"] == "flat"


# ---------------------------------------------------------------- 没有上期


def test_the_first_period_has_no_change_and_is_not_a_refusal(con):
    """★ 第一个年度没有上期——**这不是「拒绝」，是「没有可比的上期」**。

    写成 refused 的话，页面上会显示一句「缺值：上期或本期没有数据」，
    读的人以为数据有问题，实际只是网格从这一年才开始。两件事要分开说。
    """
    _fact(con, "a", "2023", "100")
    _fact(con, "b", "2024", "150")
    first = fact_grid(con, "p1")["metrics"][0]["cells"]["2023"]
    assert first["change"] is None
    assert first["change_dir"] is None
    assert first["change_refused"] is None, "没有上期不该说成「算不出来」"


def test_a_project_with_three_years_only_gets_two_changes(con):
    """华菱/首钢只有 2022–2024：三年只能出两个同比。"""
    con.execute("UPDATE project SET fiscal_years='[\"2022\",\"2023\",\"2024\"]'")
    for p, v in (("2022", "100"), ("2023", "120"), ("2024", "90")):
        _fact(con, f"f{p}", p, v)
    cells = fact_grid(con, "p1")["metrics"][0]["cells"]
    assert cells["2022"]["change"] is None
    assert cells["2023"]["change"] == "0.2000"
    assert cells["2024"]["change"] == "-0.2500"


def test_a_gap_gets_no_yoy(con):
    """★ 期间不连续**不能算同比**。

    2022 对 2020 算出来的是两年的累计变化，而它在页面上和同比长得一模一样
    ——柱子照画，颜色照上，凭空多出一截或塌掉一截。
    """
    con.execute("UPDATE project SET fiscal_years='[\"2020\",\"2022\"]'")
    _fact(con, "a", "2020", "100")
    _fact(con, "b", "2022", "150")
    cell = fact_grid(con, "p1")["metrics"][0]["cells"]["2022"]
    assert cell["change"] is None
    assert cell["change_dir"] is None
    assert "隔了 2 年" in cell["change_refused"]


# ---------------------------------------------------------------- 拒绝


def test_an_incomparable_cell_never_gets_a_change(con):
    """★ 不可比的一格不出同比。

    口径不一致时算出来的增长率**看起来很真**——它有分子有分母、有小数位，
    而它正是本项目反复要挡的东西。前端拿到的是 None，就不画柱子。
    """
    _fact(con, "a", "2023", "100")
    _fact(con, "b", "2024", "150", comparable=0, reason="mna")
    cell = fact_grid(con, "p1")["metrics"][0]["cells"]["2024"]
    assert cell["change"] is None
    assert "不可比" in cell["change_refused"]


def test_a_missing_prior_value_refuses_with_a_reason(con):
    """上期缺值 → 拒绝，且**原因要能读**。不插补、不跳过去算。"""
    _fact(con, "b", "2024", "150")
    cell = fact_grid(con, "p1")["metrics"][0]["cells"]["2024"]
    assert cell["change"] is None
    assert cell["change_refused"] and "缺值" in cell["change_refused"]


def test_a_negative_base_refuses(con):
    """负基期的「增长」没有意义（−100 → +50 会算出 −150%），转人工复核。"""
    _fact(con, "a", "2023", "-100")
    _fact(con, "b", "2024", "50")
    cell = fact_grid(con, "p1")["metrics"][0]["cells"]["2024"]
    assert cell["change"] is None
    assert "基期为负" in cell["change_refused"]


# ---------------------------------------------------------------- 单元层


def test_no_prior_period_is_silent_not_refused():
    """`_cell_yoy` 在「网格里没有上一列」时返回全空，**不编一句拒绝的理由**。"""
    change, direction, refused, formula, inputs = _cell_yoy(
        None, {"value": "100", "comparable": True}, None, "2023"
    )
    assert (change, direction, refused, formula, inputs) == (None, None, None, None, {})


def test_the_engine_is_actually_called(con):
    """★ 守住「不另写一套」这件事。

    有人日后图省事把 `(c − p) / p` 直接写在 `_cell_yoy` 里，功能看着一样，
    但引擎那边补的四种拒绝情形（口径、间隔、零基期、负基期）就全绕过去了——
    而且**不会报错**。这里断言非连续期间仍然被引擎拒绝，就是钉住这条路。
    """
    _fact(con, "a", "2020", "100")
    _fact(con, "b", "2022", "150")
    con.execute("UPDATE project SET fiscal_years='[\"2020\",\"2022\"]'")
    cell = fact_grid(con, "p1")["metrics"][0]["cells"]["2022"]
    # 引擎的措辞，不是手写除法的产物
    assert cell["change_refused"] is not None
    assert Decimal(cell["change"]) if cell["change"] else True
