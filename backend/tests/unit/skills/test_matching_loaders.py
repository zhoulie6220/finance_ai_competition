"""两个「库里没有的东西要自己补回来」的装载器。

这两个 bug 长得一模一样，所以放在一起测：

| | 现象 | 根因 |
|---|---|---|
| R | 只填了 1 项，R 算出 1/1 = 100% | 没录入的格子没补 `pending` |
| Q2 | 只抄了 3 年，页面显示 **2/2 已完成** | 没录入的年度没补 `pending` |

两边都是「**比例看起来完美，恰恰因为大部分没做**」。
R 先踩到并修了，Q2 漏了一轮——2026-10-09 补上。

> ⚠ 为什么会漏进库：`export_input_templates.py --import` 刻意丢掉还是
> `pending` 的行（丢了不损失任何人的判定）。那是对的，代价就是装载器
> 必须**按项目的年度把格子补回来**，不能只读库里有的行。
"""

from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.db.session import connect, init_schema, load_seeds


@pytest.fixture()
def con(tmp_path, monkeypatch):
    """一份临时库：建表 + 种子。**不放任何项目数据**，由用例自己插。"""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    c = connect()
    init_schema(c)
    load_seeds(c)
    c.commit()
    yield c
    c.close()
    get_settings.cache_clear()


def _project(con, project_id: str, years: list[str]) -> None:
    con.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code,"
        " industry, fiscal_years, created_at)"
        " VALUES (?, ?, ?, ?, 'steel', ?, '2026-10-09T00:00:00')",
        (project_id, project_id, project_id, "000000.SZ",
         json.dumps(years)),
    )
    con.commit()


def _aging(con, project_id: str, period: str, status: str = "validated") -> None:
    con.execute(
        "INSERT INTO q2_aging (id, project_id, period, receivable_gross,"
        " over_one_year, revenue, status, created_at)"
        " VALUES (?, ?, ?, '100', '20', '1000', ?, '2026-10-09T00:00:00')",
        (f"q-{project_id}-{period}", project_id, period, status),
    )
    con.commit()


# ============================================================ Q2 的年度要补齐


def test_missing_aging_years_are_padded_as_pending(con) -> None:
    """★ 库内只有 3 年，装载出来必须是 10 年。

    只返回库内那 3 年的话，「还有 7 年没抄」在数据里**没有痕迹**——
    配对时直接被跳过，Q 被算成「已核验」。
    """
    from app.skills.matching import _load_q2_aging

    _project(con, "p-x", [str(y) for y in range(2015, 2025)])
    for period in ("2022", "2023", "2024"):
        _aging(con, "p-x", period)

    rows = _load_q2_aging(con, "p-x")
    assert [r.period for r in rows] == [str(y) for y in range(2015, 2025)]
    statuses = {r.period: r.status for r in rows}
    assert statuses["2022"] == statuses["2023"] == statuses["2024"] == "validated"
    assert all(statuses[y] == "pending" for y in ("2015", "2016", "2021"))


def test_padded_years_make_q_incomplete(con) -> None:
    """★ 补完 pending 之后，Q 必须是「不可算」，不是 0.000000。

    `Q = 0` 读起来是「三项检查一项都没触发」，而真实情况是
    「七年还没人去抄」——两件事要做的事完全不同。
    """
    from app.engine.q2 import judge_q2, to_component
    from app.skills.matching import _load_q2_aging

    _project(con, "p-x", [str(y) for y in range(2015, 2025)])
    for period in ("2022", "2023", "2024"):
        _aging(con, "p-x", period)

    rows = _load_q2_aging(con, "p-x")
    outcomes = [judge_q2(rows[i - 1], rows[i]) for i in range(1, len(rows))]
    assert len(outcomes) == 9, "10 个年度配 9 对"

    comp = to_component(outcomes)
    assert comp.verified is False
    assert comp.value is None, "不可算时不许给 0 —— 0 在中性证据那里是「无异议」"


def test_a_fully_entered_project_is_untouched(con) -> None:
    """★ 年度抄齐了的项目，装载结果与从前逐字相同。

    宝钢 10 年 10 行、华菱 9 年 9 行都是齐的。补格子的逻辑若把它们的
    结果改掉了一个字节，两家的分数就会动——而这条改动本来只该碰首钢。
    """
    from app.skills.matching import _load_q2_aging

    _project(con, "p-full", ["2023", "2024"])
    _aging(con, "p-full", "2023")
    _aging(con, "p-full", "2024")

    rows = _load_q2_aging(con, "p-full")
    assert [(r.period, r.status) for r in rows] == [
        ("2023", "validated"), ("2024", "validated"),
    ]


def test_years_outside_fiscal_years_are_not_dropped(con) -> None:
    """★ 年度取「声明年度」与「库内已有年度」的并集。

    只按 `fiscal_years` 展开的话，一行录在声明之外的账龄数据会被静默丢掉
    —— 数据还在库里，只是再也算不到，而页面上看不出少了什么。
    """
    from app.skills.matching import _load_q2_aging

    _project(con, "p-x", ["2023", "2024"])
    _aging(con, "p-x", "2019")

    rows = _load_q2_aging(con, "p-x")
    assert [r.period for r in rows] == ["2019", "2023", "2024"]


# ======================================================== 合并范围断点从配置来


def test_scope_break_is_read_from_rule_config(con) -> None:
    """★ 断点年从 `rule_config` 读，不在代码里写死。

    种子里有首钢（p-000959 → 2015），所以拿得到；没声明的项目拿不到值。
    """
    from app.skills.matching import config_from_rules

    assert config_from_rules(con, "p-000959").scope_break_year == "2015"
    assert config_from_rules(con, "p-600019").scope_break_year is None
    assert config_from_rules(con, "p-000932").scope_break_year is None


def test_config_without_a_project_id_has_no_break(con) -> None:
    """★ 不传项目 = 不启用断点。

    这是「默认关闭」的落点：老调用方（以及任何忘了传 project_id 的地方）
    拿到的行为必须和加这个功能之前一模一样。
    """
    from app.skills.matching import config_from_rules

    assert config_from_rules(con).scope_break_year is None
