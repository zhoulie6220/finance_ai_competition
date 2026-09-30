"""三表勾稽校验的测试。

场景
----
一、资产 = 负债 + 所有者权益
二、现金滚动
三、现金流量表三项活动合计
四、毛利校验
五、不可比与缺数据的区别
六、覆盖率与整体结论
七、可复现性
八、逻辑错误的防线（恒等式退化成同义反复）

⚠ 真实数据上四条规则**要么通过、要么缺数据**，没有一条会报不平（除了
cf_components 那个已知的字典缺口）。所以「能报出哪一年不平、差多少」这个
核心功能**必须用合成用例测**，只对着真实数据测等于没测。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.checks import (
    BS_EQUATION,
    CASH_ROLLFORWARD,
    CF_COMPONENTS,
    FAILED,
    GROSS_PROFIT_CHECK,
    PASSED,
    SKIPPED_INCOMPARABLE,
    SKIPPED_MISSING_DATA,
    ChecksConfig,
    Fact,
    PeriodInput,
    check_bs_equation,
    check_cash_rollforward,
    check_cf_components,
    check_gross_profit,
    run_checks,
)

D = Decimal
CFG = ChecksConfig()


# ---------------------------------------------------------------- 构造helper


def fact(
    metric_key: str,
    value: str,
    *,
    fact_id: str | None = None,
    comparable: bool = True,
    reason: str | None = None,
) -> Fact:
    return Fact(
        fact_id=fact_id or f"f-{metric_key}",
        metric_key=metric_key,
        value=D(value),
        comparable=comparable,
        incomparable_reason=reason,
    )


def period(name: str, *facts: Fact, scope: str = "consolidated") -> PeriodInput:
    return PeriodInput(period=name, scope=scope, facts=tuple(facts))


# 宝钢 2014 年真实数据：228,652.514012 = 104,447.687866 + 124,204.826147
# （实际差 0.000001，是分位舍入，在容差内）
BALANCED_2014 = period(
    "2014",
    fact("total_assets", "228652.514012"),
    fact("total_liabilities", "104447.687866"),
    fact("total_equity", "124204.826147"),
)


# ---------------------------------------------------------------- 一、资产负债表恒等式


def test_balanced_sheet_passes():
    """真实数据必须通过——反例测试也要有正例，否则「拦得太死」看不出来。"""
    r = check_bs_equation(BALANCED_2014, CFG)
    assert r.status == PASSED
    assert r.ok is True
    assert r.diff == D("-0.000001")          # 负债+权益比资产多 1 元的分位舍入


def test_unbalanced_sheet_reports_year_and_delta():
    """★ 核心功能：报出是哪一年、差多少。

    真实数据全平，这条只能靠合成用例。差 10000 百万元（100 亿），
    远超 300000 × 0.5% = 1500 的容差。
    """
    p = period(
        "2019",
        fact("total_assets", "300000"),
        fact("total_liabilities", "200000"),
        fact("total_equity", "90000"),      # 少了 10000
    )
    r = check_bs_equation(p, CFG)
    assert r.status == FAILED
    assert r.ok is False
    assert r.period == "2019"
    assert r.diff == D("10000.000000")
    assert r.tolerance == D("1500.000000")   # 基准取两边最大值 300000 × 0.005
    assert "不平" in r.message
    assert "2019" in r.message
    # 建议要指向解析而不是造假
    assert "解析" in (r.suggestion or "")


def test_imbalance_within_tolerance_is_accepted():
    """容差以内（0.5%）的不平衡判通过——这是 rule_config 里会计定的口径。

    刻意写成测试而不是听任它藏在实现里：0.5% 的资产对宝钢就是十几亿，
    听起来很宽。但真实数据上偏差是 1e-6 量级，这个松容差的作用是吸收
    舍入与披露口径差异，不是为了让明显错误溜过去。真要收紧就改
    rule_config 的 check.balance_tolerance，**不要改代码里的默认值**——
    两者分叉之后，页面上显示的口径和实际生效的口径就对不上了。
    """
    p = period(
        "2019",
        fact("total_assets", "300000"),
        fact("total_liabilities", "200000"),
        fact("total_equity", "99000"),      # 差 1000 < 容差 1500
    )
    assert check_bs_equation(p, CFG).status == PASSED


def test_equity_falls_back_to_parent_plus_minority():
    """没有合计行时用「归母 + 少数股东权益」——两个各自独立披露的组成部分相加。

    这不是反推：反推是拿「资产 − 负债」算出权益，那样等式恒成立。
    """
    p = period(
        "2018",
        fact("total_assets", "335140.605812"),
        fact("total_liabilities", "145895.516312"),
        fact("equity_parent", "176762.553945"),
        fact("minority_interest", "12482.535554"),
    )
    r = check_bs_equation(p, CFG)
    assert r.status == PASSED
    assert "归属于母公司股东权益 + 少数股东权益" in r.formula
    assert set(r.inputs) == {
        "f-total_assets",
        "f-total_liabilities",
        "f-equity_parent",
        "f-minority_interest",
    }


def test_tolerance_is_relative_to_the_larger_side():
    """容差按相对比例。差 0.4% 在 0.5% 容差内应通过，0.6% 应不通过。"""
    base = dict(assets="1000000", liabilities="600000")
    ok = check_bs_equation(
        period("2020", fact("total_assets", base["assets"]),
               fact("total_liabilities", base["liabilities"]),
               fact("total_equity", "396000")), CFG)          # 差 0.4%
    bad = check_bs_equation(
        period("2020", fact("total_assets", base["assets"]),
               fact("total_liabilities", base["liabilities"]),
               fact("total_equity", "394000")), CFG)          # 差 0.6%
    assert ok.status == PASSED
    assert bad.status == FAILED


def test_missing_equity_is_not_a_pass():
    """缺数据**不等于通过**。这是整个校验层最容易写错的一条。"""
    p = period(
        "2019",
        fact("total_assets", "300000"),
        fact("total_liabilities", "200000"),
    )
    r = check_bs_equation(p, CFG)
    assert r.status == SKIPPED_MISSING_DATA
    assert r.ok is False
    assert r.evaluable is False


def test_equity_composed_of_one_part_only_is_missing():
    """只有归母权益、没有少数股东权益时不能凑数——那会把少数股东权益当成 0。"""
    p = period(
        "2022",
        fact("total_assets", "300000"),
        fact("total_liabilities", "200000"),
        fact("equity_parent", "100000"),
    )
    r = check_bs_equation(p, CFG)
    assert r.status == SKIPPED_MISSING_DATA


# ---------------------------------------------------------------- 二、现金滚动


def test_cash_rollforward_passes_exactly():
    """真实数据上这条精确成立，是覆盖率最好的一条。"""
    p = period(
        "2024",
        fact("cash_begin", "1000"),
        fact("cash_net_increase", "-468.530275"),
        fact("cash_end", "531.469725"),
    )
    r = check_cash_rollforward(p, CFG)
    assert r.status == PASSED
    assert r.diff == D("0.000000")


def test_cash_rollforward_catches_a_parse_error():
    """期初数被取成母公司口径时，差额会很明显。"""
    p = period(
        "2023",
        fact("cash_begin", "1000"),
        fact("cash_net_increase", "500"),
        fact("cash_end", "1400"),          # 应为 1500，少 100
    )
    r = check_cash_rollforward(p, CFG)
    assert r.status == FAILED
    assert r.diff == D("-100.000000")
    assert r.period == "2023"
    assert "现金滚动不平" in r.message


def test_cash_rollforward_scale_uses_largest_item_not_the_end_balance():
    """期末现金接近零时，拿它当分母会让相对误差爆掉。

    这里期末 1，期初 100000、净增 -99999：按「最大绝对值」算基准是 100000，
    差为 0，通过。按期末算则会得到一个巨大的相对误差。
    """
    p = period(
        "2021",
        fact("cash_begin", "100000"),
        fact("cash_net_increase", "-99999"),
        fact("cash_end", "1"),
    )
    r = check_cash_rollforward(p, CFG)
    assert r.status == PASSED


# ---------------------------------------------------------------- 三、现金流量表三项活动


def test_three_activities_match_when_no_fx():
    """没有汇率影响时三项活动合计应当恰好等于净增加额。"""
    p = period(
        "2024",
        fact("cash_net_increase", "700"),
        fact("cfo", "1000"),
        fact("cfi", "-500"),
        fact("cff", "200"),
    )
    r = check_cf_components(p, CFG)
    assert r.status == PASSED


def test_fx_residual_is_reported_as_a_dictionary_gap_not_a_parse_error():
    """★ 残差等于汇率变动影响时，建议里必须点名那一行，而不是让人去查解析。

    宝钢 2024 实测：净增加 −468.530275，三项合计 −176.414877，差 −292.115398。
    年报现金流量表在两者之间有一行「四、汇率变动对现金及现金等价物的影响」。
    """
    p = period(
        "2024",
        fact("cash_net_increase", "-468.530275"),
        fact("cfo", "1000"),
        fact("cfi", "-1176.414877"),
        fact("cff", "0"),
    )
    r = check_cf_components(p, CFG)
    assert r.status == FAILED
    assert "汇率变动" in r.message
    assert "汇率变动对现金及现金等价物的影响" in (r.suggestion or "")
    assert "字典" in (r.suggestion or "")
    # 严重度是 warn 而不是 error——这不是报表本身不平
    assert r.severity == "warn"


# ---------------------------------------------------------------- 四、毛利


def test_gross_profit_is_skipped_when_never_disclosed():
    """本批数据上毛利是派生指标、没有事实行，所以这条永远缺数据。"""
    p = period(
        "2024",
        fact("revenue", "322116"),
        fact("operating_cost", "290000"),
    )
    r = check_gross_profit(p, CFG)
    assert r.status == SKIPPED_MISSING_DATA
    assert r.evaluable is False


def test_gross_profit_uses_revenue_not_total_revenue():
    """★ 必须用营业收入，不是营业总收入。

    2024 年宝钢两者恰好相等，只看那一年发现不了错配；2015 年差 0.3%，
    在 0.5% 容差内也过得去——所以这条错配**几乎不可能被现有容差抓到**。
    这个测试把「用的是哪个键」钉死在公式文本里。
    """
    p = period(
        "2015",
        fact("gross_profit", "30000"),
        fact("revenue", "100000"),
        fact("operating_cost", "70000"),
        fact("total_revenue", "100300"),     # 与 revenue 不同
    )
    r = check_gross_profit(p, CFG)
    assert r.status == PASSED
    assert r.rhs == D("30000")
    assert "revenue" in r.formula
    assert "营业总收入" in r.formula       # 公式里点明「不是」哪一个


def test_gross_profit_catches_wrong_cost_line():
    """营业成本误取「营业总成本」时差额会很大——那是含期间费用的合计行。"""
    p = period(
        "2020",
        fact("gross_profit", "30000"),
        fact("revenue", "100000"),
        fact("operating_cost", "85000"),     # 混进了期间费用
    )
    r = check_gross_profit(p, CFG)
    assert r.status == FAILED
    assert r.diff == D("15000.000000")
    assert "营业总成本" in (r.suggestion or "")


# ---------------------------------------------------------------- 五、不可比 vs 缺数据


def test_incomparable_is_distinct_from_missing():
    """不可比是**已知且确认过**的口径问题；缺数据是**还不知道**。

    混在一起报会让人以为后者也已经确认过了。两者都不通过，但理由必须不同。
    """
    p = period(
        "2021",
        fact("total_assets", "300000"),
        fact("total_liabilities", "200000", comparable=False, reason="mna"),
        fact("total_equity", "100000"),
    )
    r = check_bs_equation(p, CFG)
    assert r.status == SKIPPED_INCOMPARABLE
    assert "mna" in r.message
    assert r.ok is False


def test_incomparable_message_names_the_reason():
    """不可比必须写原因——没有原因时也要显式说出来，不能留空。"""
    p = period(
        "2021",
        fact("total_assets", "300000"),
        fact("total_liabilities", "200000", comparable=False),
        fact("total_equity", "100000"),
    )
    r = check_bs_equation(p, CFG)
    assert r.status == SKIPPED_INCOMPARABLE
    assert "未注明原因" in r.message


# ---------------------------------------------------------------- 六、覆盖率与整体结论


def test_rules_are_independent():
    """一条规则缺数据不影响其他条——否则覆盖矩阵会失真。"""
    p = period(
        "2014",
        fact("total_assets", "1000"),
        fact("total_liabilities", "400"),
        fact("total_equity", "600"),
        # 现金三项全缺
    )
    report = run_checks([p], project_id="p1")
    by_rule = {o.rule_key: o for o in report.outcomes}
    assert by_rule[BS_EQUATION].status == PASSED
    assert by_rule[CASH_ROLLFORWARD].status == SKIPPED_MISSING_DATA
    assert report.evaluable_count == 1
    assert report.total_count == 4


def test_coverage_line_never_reads_as_all_passed():
    """★ 「33 项里 10 项可评估、全过」绝不能被读成「33 项全过」。

    这是本项目最怕的那种误读：数字是对的，但结论被夸大。
    """
    p = period(
        "2014",
        fact("total_assets", "1000"),
        fact("total_liabilities", "400"),
        fact("total_equity", "600"),
    )
    line = run_checks([p], project_id="p1").coverage_line()
    assert "1 项可评估" in line
    assert "4 项期间校验" in line


def test_hard_and_soft_failures_are_distinguished():
    """报表不平（error）与字典缺字段（warn）含义完全不同，不能混着报。

    混在一起会让人把「字典该补一个字段」读成「公司报表有问题」。
    """
    bad_bs = period(
        "2019",
        fact("total_assets", "300000"),
        fact("total_liabilities", "200000"),
        fact("total_equity", "90000"),
    )
    r = run_checks([bad_bs], project_id="p1")
    assert len(r.hard_failures) == 1
    assert r.hard_failures[0].rule_key == BS_EQUATION
    assert r.sheet_ok is False


def test_dictionary_gap_does_not_make_the_sheet_look_wrong():
    """汇率残差属于 warn：恒等式确实不成立，但报表本身是平的。

    宝钢 2017–2024 全部是这种情况——21 项可评估里 8 项「不平」，但那 8 项
    都是同一个字典缺口。这个区分决定了页面上的读法完全不同。
    """
    p = period(
        "2024",
        fact("total_assets", "1000"),
        fact("total_liabilities", "400"),
        fact("total_equity", "600"),
        fact("cash_net_increase", "700"),
        fact("cfo", "1000"),
        fact("cfi", "-500"),
        fact("cff", "300"),
    )
    r = run_checks([p], project_id="p1")
    assert len(r.soft_failures) == 1
    assert r.soft_failures[0].rule_key == CF_COMPONENTS
    assert r.hard_failures == ()
    assert r.sheet_ok is True
    assert "字典缺字段" in r.coverage_line()


def test_ok_is_true_only_when_nothing_failed():
    """缺数据不算失败，但也不算成功——两句话都要能同时说出口。"""
    ok_only = run_checks([BALANCED_2014], project_id="p1")
    assert ok_only.failures == ()
    assert ok_only.ok is True
    assert ok_only.evaluable_count == 1      # 只有资产负债表那条可评估

    bad = run_checks(
        [period("2019", fact("total_assets", "1000"),
                fact("total_liabilities", "400"),
                fact("total_equity", "500"))],
        project_id="p1",
    )
    assert bad.ok is False
    assert len(bad.failures) == 1


def test_report_covers_every_rule_for_every_period():
    report = run_checks([BALANCED_2014, BALANCED_2014], project_id="p1")
    assert report.total_count == 8           # 2 个期间 × 4 条规则
    assert len(report.outcomes) == len(report.periods) * 4


# ---------------------------------------------------------------- 七、可复现性


def test_same_input_same_output():
    """纯函数：同输入必同输出。冻结数据类的相等性就够证明。"""
    assert check_bs_equation(BALANCED_2014, CFG) == check_bs_equation(BALANCED_2014, CFG)
    assert run_checks([BALANCED_2014], project_id="p1") == run_checks(
        [BALANCED_2014], project_id="p1"
    )


def test_every_outcome_carries_formula_for_the_evidence_chain():
    """「点击结论回到计算过程」靠的是 formula + inputs，缺一不可。"""
    report = run_checks([BALANCED_2014], project_id="p1")
    for o in report.outcomes:
        assert o.formula, f"{o.rule_key} 缺 formula"
    passed = [o for o in report.outcomes if o.status == PASSED]
    assert passed
    for o in passed:
        assert o.inputs, f"{o.rule_key} 通过但没有可回溯的 fact_id"


# ---------------------------------------------------------------- 八、逻辑错误的防线


def test_tautological_equation_is_rejected():
    """★ 恒等式两边共用同一笔事实 = 等式退化成同义反复，永远成立。

    最典型的写法是拿「资产 − 负债」当权益。它会永远通过、永远抓不住解析错误，
    而且**不报任何错**——正是本项目最怕的那类缺陷。所以这里抛 ValueError
    而不是返回失败状态：这是代码写错了，不是数据有问题。
    """
    shared = fact("total_assets", "1000", fact_id="same-id")
    p = period(
        "2019",
        shared,
        fact("total_liabilities", "400"),
        fact("total_equity", "600", fact_id="same-id"),   # 与资产同一笔
    )
    with pytest.raises(ValueError, match="同义反复"):
        check_bs_equation(p, CFG)


def test_config_rejects_absurd_tolerance():
    """容差 ≥ 1 等于允许 100% 以上的偏差，什么都能通过——那是废掉规则。"""
    with pytest.raises(ValueError):
        ChecksConfig(balance_tolerance=D("1"))
    with pytest.raises(ValueError):
        ChecksConfig(balance_tolerance=D("0"))
    with pytest.raises(ValueError):
        ChecksConfig(cash_rollforward_tolerance=D("-0.1"))
