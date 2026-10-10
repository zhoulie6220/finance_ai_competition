"""经营计划标题里的目标年度，必须落到它下面的句子上。

★ 这一条救的是一批**看起来完全正常**的错判。2017 年报起，首钢的财务预算句写成：

    (2)财务指标预算安排
    营业收入614.77亿元，同比增长2.03%。

句子本身**一个年份都没有**，只有「同比」；年份在上一行的标题「3、2018年经营计划」里。
标题行会被 `iter_sentences` 丢掉（它没有谓语，不是句子），所以那个年份只能在
扫行的过程中往下传——事后回 `text` 里找是找不到的。

不传的后果：这条 2018 年的计划被锚到**报告年 2017**，`bucket` 判成 `c` 而不是 `h`，
于是它进了「当期一致性」，拿 2017 年的实际去核一个 2018 年的计划。
**不报错，只是年份是错的。**
"""

from __future__ import annotations

from app.engine.claim_rules import match_plan_budget
from app.parsing.claims import iter_sentences, plan_year_state

#: 2017 年报里的那一段（折行已接好）。
SECTION = """九、公司未来发展的展望
1、行业格局和趋势
2018年是深入贯彻党的十九大精神的开局之年，钢铁行业深入调整。
3、2018年经营计划
(1)主要产品产量
①迁钢公司：铁713万吨，同比下降3.9%；钢730万吨，同比下降2.7%。
(2)财务指标预算安排
营业收入614.77亿元，同比增长2.03%。
(3)资金收支预算安排
资金流量预算收入1063.08亿元。
4、可能面对的风险
(1)政策及行业风险
随着国内新基地、新产线项目的达产达效，公司将面临更为强劲的竞争压力。
"""


def _by_text(sents, needle):
    return next(s for s in sents if needle in s.text)


def test_budget_sentence_inherits_the_plan_year_from_the_heading():
    sents = iter_sentences(SECTION)
    assert _by_text(sents, "营业收入614.77亿元").plan_year == "2018"


def test_sub_headings_do_not_clear_the_plan_year():
    """`(1)主要产品产量` 是**子标题**——它在计划标题之下，不能把年份清掉。

    清掉的后果不是报错，是那个预算句又变回「没有年份」。
    """
    sents = iter_sentences(SECTION)
    assert _by_text(sents, "铁713万吨").plan_year == "2018"


def test_another_top_level_heading_ends_the_plan():
    """`4、可能面对的风险` 之后就不在计划段里了——年份必须清掉。

    不清的话，风险章节里的句子会顶着一个经营计划的目标年度，
    而那个年度看起来完全合理。
    """
    sents = iter_sentences(SECTION)
    assert _by_text(sents, "竞争压力").plan_year is None


def test_the_year_carries_across_sections():
    """★ 跨页延续：首钢 2015 年报的标题在物理 p17、预算句在 p18。

    章节是按页切的，不带过去就拿不到年份，那条 2016 年预算会解析不出来
    ——而它是**必须被抽出来、再由过渡年规则显式排除**的那一条。
    """
    page_17 = "3、2016年经营计划\n(1)主要产品产量\n①迁钢公司:铁703万吨，同比下降5.51%。"
    page_18 = "(3)财务指标预算安排\n2016年营业收入327.01亿元，同比下降11.9%。"
    carry = plan_year_state(page_17)
    assert carry == "2016"
    sents = iter_sentences(page_18, plan_year=carry)
    assert _by_text(sents, "327.01亿元").plan_year == "2016"


def test_a_new_annual_report_starts_clean():
    """换一份年报就清空——带过去会给一整批句子锚错年份，而且不报错。"""
    assert plan_year_state("一、概述\n公司全年实现营业收入100亿元。", None) is None


# ---------------------------------------------------------------- 认哪些句


def test_needs_a_plan_year_to_recognise_a_budget():
    """**拿不到计划年就返回 None，绝不猜。**"""
    assert match_plan_budget("营业收入614.77亿元，同比增长2.03%。", None) is None


def test_recognises_the_revenue_budget_under_a_plan_heading():
    m = match_plan_budget("营业收入614.77亿元，同比增长2.03%。", "2018")
    assert m is not None
    assert m.rule.claim_type == "management_budget"
    assert m.rule.primary_metric == "revenue"


def test_cash_flow_budget_is_not_a_revenue_budget():
    """★「资金流量预算收入825.77亿元」含「预算收入」，但它不是**营业收入**。

    它和营业收入摆在相邻的两行，长得几乎一样。认成一条的话，
    825.77 亿会被拿去和营业收入比——偏差算得出来，理由写得出。
    """
    assert match_plan_budget("资金流量预算收入825.77亿元。", "2016") is None
    assert match_plan_budget("其中:经营收入419.35亿元。", "2016") is None


def test_theme_matching_never_picks_up_the_budget_rule():
    """★ 预算主题的触发词是**空的**——它只由 `match_plan_budget` 认。

    给它一组触发词的话，`match_theme` 会把年报里**每一句「营业收入」**
    都收进来，而其中绝大多数讲的是本年的实绩、不是计划。
    实绩被当成计划去核验，就是「拿事实核验事实」，永远判支持。
    """
    from app.engine.claim_rules import match_theme

    assert match_theme("2022年公司实现营业收入1181.42亿元。") is None
    assert match_plan_budget("2022年公司实现营业收入1181.42亿元。", "2023") is not None
