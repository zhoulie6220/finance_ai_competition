"""字段字典的一致性回归测试。

字段字典是「PDF 行名 → 字段键」映射的唯一依据，它的错误**不会报错**：
一个别名漏掉，那个字段静默解析不出来；一个排除词写错，两个字段对着同一行
争抢，谁赢取决于遍历顺序。所以这里的断言全部针对「静默损坏」这一类缺陷。

规则本体在 `app.db.dictionary.validate()`，由三处共用：本文件、`scripts/dict_csv.py`
和建库流程。放在一处而不是各写一遍，是因为「校验逻辑分散」本身就是漏检的常见
来源——测试里加了一条规则、导入脚本没加，改字典时照样能把坏数据放进去。

所以本文件分两部分：
  1. 真实种子数据必须通过全部规则
  2. **每条规则都要能抓到它该抓的东西**——用构造的坏字典验证，
     否则「规则没生效」和「数据恰好干净」看起来一模一样
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.db.dictionary import fetch, validate
from app.db.session import connect_memory, init_schema, load_seeds


@pytest.fixture()
def con() -> sqlite3.Connection:
    """装载真实种子数据的字典。断言的是种子文件本身，不是构造的样例。"""
    c = connect_memory()
    init_schema(c)
    load_seeds(c)
    yield c
    c.close()


def _mutate(con: sqlite3.Connection, key: str, **cols: object) -> None:
    assignments = ", ".join(f"{c}=?" for c in cols)
    con.execute(
        f"UPDATE metric_definition SET {assignments} WHERE metric_key=?",
        (*cols.values(), key),
    )


def _has(problems: list[str], *fragments: str) -> bool:
    return any(all(f in p for f in fragments) for p in problems)


# --------------------------------------------------------------- 真实数据


def test_seed_loads_and_is_not_empty(con: sqlite3.Connection) -> None:
    assert len(fetch(con)) >= 80


def test_real_dictionary_passes_all_rules(con: sqlite3.Connection) -> None:
    """种子文件本身必须是干净的。失败时打印全部问题，不要只报第一条。"""
    problems = validate(con)
    assert not problems, "字段字典有问题：\n  " + "\n  ".join(problems)


# --------------------------------------------------------------- 反例：每条规则都要能抓到东西


def test_catches_exclusion_blocking_own_alias(con: sqlite3.Connection) -> None:
    """排除词与自己的别名重合 —— 该字段永远映射不上。

    映射语义是**行名精确匹配别名、排除词也是精确匹配行名**。
    在这套语义下重合就意味着命中不了，所以必须拦。
    """
    _mutate(con, "revenue", exclusion_terms='["营业收入"]')
    assert _has(validate(con), "revenue", "排除词挡住了自己的别名")


def test_catches_alias_shared_within_same_industry(con: sqlite3.Connection) -> None:
    """同一别名挂到两个字段上 —— 谁赢取决于遍历顺序，结果不可复现。"""
    _mutate(con, "steel_output", aliases='["钢材产量", "钢材销量"]')
    assert _has(validate(con), "钢材销量", "被多个字段共用")


def test_cross_industry_alias_clash_is_allowed(con: sqlite3.Connection) -> None:
    """跨行业重名**不是**错误：「销售量」钢企和煤企年报里都出现。

    这一条防的是矫枉过正——把规则收得太紧，逼着会计同学去编不存在的别名。
    正确做法是映射时先按 project.industry 过滤候选字段。
    """
    _mutate(con, "steel_sales_volume", aliases='["钢材销量", "销售量"]')
    _mutate(con, "coal_sales_volume", aliases='["商品煤销量", "销售量"]')
    assert not _has(validate(con), "销售量"), "跨行业重名被误判为冲突"


def test_catches_text_metric_with_numeric_unit(con: sqlite3.Connection) -> None:
    """文本型字段声明了数值单位 —— 解析层会试图把它换算成百万元。"""
    _mutate(con, "audit_opinion", unit_kind="currency")
    assert _has(validate(con), "audit_opinion", "文本型却声明了数值单位")


def test_catches_disclosure_metric_that_is_not_text(con: sqlite3.Connection) -> None:
    """披露事项是无数值的抽取目标，不能声明成数值型。

    否则解析层会往 financial_fact 里写——而硬规则一要求 validated 行必须有
    value_millions，文本行进去就只能永远停在 needs_review。
    """
    _mutate(con, "accounting_policy_change", value_type="flow", unit_kind="currency")
    assert _has(validate(con), "accounting_policy_change", "披露事项")


def test_parent_key_pointing_nowhere_is_rejected_by_the_database(
    con: sqlite3.Connection,
) -> None:
    """父字段不存在 —— 由**外键**拦，不是由 validate() 拦。

    这条断言故意打在数据库层：`parent_key` 有外键，所以等 validate() 跑到时
    已经不可能有不存在的父字段了。validate() 里因此**没有**这条规则——
    留着一段永远跑不到的校验，比没有它更糟，它会让下一个人以为那里查过了。

    面向会计同学的友好提示（带 Excel 行号，而不是甩一句 FOREIGN KEY constraint
    failed）在 `scripts/dict_csv.py::_preflight` 里，那才是能跑到的地方。
    """
    with pytest.raises(sqlite3.IntegrityError):
        _mutate(con, "net_income_parent", parent_key="net_profit_typo")


def test_catches_example_without_source_marker(con: sqlite3.Connection) -> None:
    """有例句却标了来源 —— 分不清这句是年报原文还是合成的标准句。"""
    _mutate(con, "revenue", example_source=None)
    assert _has(validate(con), "revenue", "没有标注 example_source")


def test_catches_source_marker_without_example(con: sqlite3.Connection) -> None:
    """反过来也不行：标了来源却没有句子。"""
    _mutate(con, "inventory", example_sentence=None)
    assert _has(validate(con), "inventory", "没有例句却标了 example_source")


def test_catches_placeholder_marked_as_annual_report(con: sqlite3.Connection) -> None:
    """标成「年报原文」却还留着【】占位符 —— 等于把合成句当成了真证据。

    这正是比赛「明确区分事实与观点」要求的那条线：占位符句不是事实。
    """
    # 出处必须一起填，否则先被下面那条「年报原文必须有出处」的 CHECK 拦下，
    # 就测不到占位符这一条了——两条规则都得能单独触发。
    _mutate(
        con,
        "cfo",
        example_sentence="经营活动产生的现金流量净额为【数值】元。",
        example_source="annual_report",
        example_file="600019_2024_年报.pdf",
        example_page=86,
    )
    assert _has(validate(con), "cfo", "仍有【】占位符")


def test_catches_annual_report_provenance_missing(con: sqlite3.Connection) -> None:
    """标为年报原文却没记是哪份文件的哪一页（签字文档 §6）。

    这是「例句是证据锚点」的具体落实：没有出处的原文，
    和一句编造的话在库里长得完全一样，评委无从核对。

    数据库的 CHECK 会先拦下来，所以这里断言的是 IntegrityError；
    validate() 里那条同名规则是为 CSV 导入路径准备的——那条路径在装库**之前**跑，
    能报出「第几行、哪个字段」而不是一句 constraint failed。
    """
    with pytest.raises(sqlite3.IntegrityError):
        _mutate(
            con,
            "cfo",
            example_sentence="经营活动产生的现金流量净额为 2,345,678,901.23 元。",
            example_source="annual_report",
        )


def test_catches_half_migrated_example(con: sqlite3.Connection) -> None:
    """句子还是占位符，出处却已经填上了 —— 改了一半的状态。

    这是最容易蒙混过关的一种：看起来「已经标了来源和页码」，
    但句子本身还是合成的。检出的依据是 example_sentence 里仍有【】占位符，
    与 example_source 是否为 annual_report 无关。
    """
    _mutate(
        con,
        "cfo",
        example_sentence="经营活动产生的现金流量净额为【数值】元。",
        example_source="synthetic_example",
        example_file="600019_2024_年报.pdf",
        example_page=86,
    )
    assert _has(validate(con), "cfo", "却填了 example_file")


def test_catches_annual_report_missing_sentence_but_has_provenance(
    con: sqlite3.Connection,
) -> None:
    """反向残缺：出处齐了，句子没了。"""
    _mutate(
        con,
        "cfo",
        example_sentence=None,
        example_source="annual_report",
        example_file="600019_2024_年报.pdf",
        example_page=86,
    )
    assert _has(validate(con), "cfo", "没有例句却标了 example_source")


def test_catches_invalid_json_in_aliases(con: sqlite3.Connection) -> None:
    """别名不是合法 JSON —— 解析层直接崩，而不是降级到 needs_review。"""
    _mutate(con, "inventory", aliases='["存货"')
    assert _has(validate(con), "inventory", "aliases")


def test_catches_blank_alias(con: sqlite3.Connection) -> None:
    """别名里混进空白项 —— 会匹配上一个空行名。"""
    _mutate(con, "inventory", aliases='["存货", "  "]')
    assert _has(validate(con), "inventory", "空白项")


def test_catches_parent_key_pointing_to_self(con: sqlite3.Connection) -> None:
    _mutate(con, "inventory", parent_key="inventory")
    assert _has(validate(con), "inventory", "指向自己")


def test_validate_reports_every_problem_not_just_the_first(con: sqlite3.Connection) -> None:
    """一次报全部问题。

    只报第一条的话，会计同学每改一处就要重跑一轮，89 个字段能来回几十次。
    """
    _mutate(con, "revenue", exclusion_terms='["营业收入"]')
    _mutate(con, "inventory", aliases='["存货"')
    problems = validate(con)
    assert len(problems) >= 2
    assert any("revenue" in p for p in problems)
    assert any("inventory" in p for p in problems)


# --------------------------------------------------------------- 与 CSV 往返的一致性


def test_csv_round_trip_multi_value_parsing() -> None:
    """多值列要能吃三种形态，因为三个地方都有人手写。

    漏掉任何一种，`--export` 会把 Python 的 list 直接 repr 进单元格
    （`['营业收入', '主营业务收入']`），然后 `--import` 原样写回数据库，
    整个字段静默变成一个字面量字符串。
    """
    from app.db.dictionary import _parse_multi

    assert _parse_multi(["营业收入", "主营业务收入"]) == ["营业收入", "主营业务收入"]
    assert _parse_multi('["营业收入", "主营业务收入"]') == ["营业收入", "主营业务收入"]
    assert _parse_multi("营业收入|主营业务收入") == ["营业收入", "主营业务收入"]
    assert _parse_multi("营业收入;主营业务收入") == ["营业收入", "主营业务收入"]
    assert _parse_multi("营业收入") == ["营业收入"]
    assert _parse_multi(None) == []
    assert _parse_multi("") == []


def test_render_is_idempotent(con: sqlite3.Connection) -> None:
    """渲染 → 装库 → 再渲染，两次的 SQL 必须逐字节相同。

    不幂等的话，每次 `--import` 都会产生一整份无意义的 diff（空列表写成 '[]'
    还是 NULL、多值列的分隔符带不带空格之类）。真正的改动会淹没在里面，
    会计同学和审查者都会慢慢不看 diff——那等于没有版本控制。
    """
    from app.db.dictionary import render_sql

    first = render_sql(fetch(con))

    fresh = connect_memory()
    init_schema(fresh)
    fresh.executescript(first)
    second = render_sql(fetch(fresh))
    fresh.close()

    assert first == second


def test_rendered_sql_round_trips_through_the_database(con: sqlite3.Connection) -> None:
    """字典渲染成 SQL 再读回来必须一模一样。

    **SQL 不做相邻字符串字面量拼接**（Python 会），跨行的两个 'abc' 'def'
    是语法错误；引号没转义也是。这条把「写出去」和「读回来」接成一个闭环。
    """
    from app.db.dictionary import render_sql

    rows = fetch(con)
    # 塞一个带单引号的字段，验证转义
    rows[0]["note"] = "含单引号的备注：don't break"
    body = render_sql(rows)

    fresh = connect_memory()
    init_schema(fresh)
    fresh.executescript(body)
    again = fetch(fresh)
    fresh.close()

    assert len(again) == len(rows)
    assert again[0]["note"] == "含单引号的备注：don't break"
    for before, after in zip(rows, again):
        for col in ("metric_key", "label_cn", "aliases", "exclusion_terms", "example_sentence"):
            assert before[col] == after[col], f"{before['metric_key']}.{col} 往返后变了"
