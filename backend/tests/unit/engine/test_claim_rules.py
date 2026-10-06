"""主张抽取的规则层测试。

两个 bug 是在真实语料上才暴露出来的，都**不报错**：

1. 表格剔除器把带数值的真实句子判成表格行丢掉——丢掉的恰恰是最有
   抽取价值的句子（带数值、带方向、带对象）。
2. 幅度抽取取到的是句子开头的**年份**而不是金额。判定会拿「2022」
   去和实际值比，得到一个永远对不上的偏差，把好句子判成冲突。
3. 换行合并把数字表格行和后面的正文接在一起，数字占比被稀释，
   剔除器就再也认不出来了。

三条都写成了回归测试。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.claim_rules import (
    THEMES,
    extract_direction,
    extract_magnitude,
    extract_period_expr,
    is_pending,
    match_theme,
    resolve_period,
)
from app.parsing.claims import (
    iter_sentences,
    join_wrapped_lines,
    looks_like_table_row,
)

D = Decimal


# ---------------------------------------------------------------- 表格剔除


@pytest.mark.parametrize(
    "line",
    [
        "冷轧碳钢板卷        41,655    35,695    14.31    -",
        "97.82  1042,200  80%",
        "热轧板卷   1,234   1,100   12.2",
        "919191919191919191919191020202020202020202020202121212",
    ],
)
def test_table_rows_are_rejected(line: str) -> None:
    assert looks_like_table_row(line) is True


@pytest.mark.parametrize(
    "line",
    [
        # ★ 回归：这三句最初被误判成表格行丢掉了，它们恰恰最该被抽取
        "中钢协统计数据显示，2022年全国粗钢产量10.13亿吨，同比下降2.1%；",
        "会员钢铁企业全年累计实现利润总额982亿元，同比下降72.27%。",
        "全年供需基本平衡；重点钢铁企业进口粉矿采购成本同比下降24.16%。",
        # 普通叙述句
        "本年度实现营业收入 322,116 百万元。",
        "公司坚持做精做强的战略，持续完善生产体系。",
    ],
)
def test_numeric_prose_is_not_a_table_row(line: str) -> None:
    """带数值的正文句**不是**表格行。

    判据必须靠 PDF 的列对齐（两个以上空格），不能靠「数字占比」——
    中文句子里数字被标点和空格隔开，占比自然就高，按比例判会把最有
    价值的句子全部丢掉，而且是静默丢掉。
    """
    assert looks_like_table_row(line) is False


@pytest.mark.parametrize(
    "line",
    [
        # ★ 真实数据。附注列把说明文字直接拼在数字后面，多出一个非数字格，
        # 「非数字格 ≤ 1」那条判据差一格失效；「数字占比 > 0.5」那条退路
        # 被同一段文字稀释到 0.45 也失效。两条都拦不住，于是这行进了候选句，
        # 最后出现在会计要逐条判的 P 表第一行上。
        "应收票据  627  0.2  29,190  8.7  -97.9  新金融工具准则列报项目不同所致主要为2019年执行",
        "合计  万吨  4,687  4,719  236  0.3  0.2  -2.6注：2019年度销售量中包含销售给宝日汽车板的碳钢产品196.5万吨",
        "无缝钢管  制造费用及其他  1,425,611,515.00  14.22%  1,653,194,623.00  14.22%  -13.77%说明：报告期，公司钢铁行业的制造费用及其他的",
    ],
)
def test_table_row_with_a_trailing_note_is_still_rejected(line: str) -> None:
    """★ 附注列把数字行「稀释」成正文，是同一类 bug 的第二种形态。

    前一条测试守的是「表格行吸收下一段正文」，这条守的是**说明文字本来就
    和数字在同一行**——PDF 里附注列没有分隔符，抽出来的就是一整串。
    两者都会让数字占比掉到阈值以下，而**都不报错**。
    """
    assert looks_like_table_row(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "本期费用化研发投入  3,449",
        "本期费用化研发投入  8,726",
        "本期资本化研发投入  1,234",
    ],
)
def test_two_cell_table_rows_are_rejected(line: str) -> None:
    """★ 只有两格的表格行。

    上面那条判据要求 ≥3 格，这类只有两格，**整条判据直接跳过**——
    实测宝钢 130 条候选里有 10 条是这种（研发投入表的行标签加一个数），
    会计要逐条判的表里混着它们。
    """
    assert looks_like_table_row(line) is True


@pytest.mark.parametrize(
    "line",
    [
        # ★ 这两句只差一个空格，**不能一起误杀**。
        # 分开它们的是：正文的数值带单位、句子带句号；表格行两样都没有。
        "本年度实现营业收入  322,116 百万元。",
        "营业收入  1,234 万元，同比增长 5%。",
        "公司粗钢产量  5,150 万吨。",
    ],
)
def test_two_cell_prose_with_units_is_not_a_table_row(line: str) -> None:
    assert looks_like_table_row(line) is False


def test_a_table_caption_does_not_absorb_the_next_sentence() -> None:
    """★ 「数据来源：wind资讯」是完整的块，后面的正文是另一句。

    接上去会拼出**年报上根本不存在的句子**：

        粗钢产量  CSPI月均数据来源：wind资讯公司把握国家供给侧结构改革、钢铁去产能的机遇…

    而 `claim_text` 是证据链的终点——它必须是年报里真实存在的那一句。
    拼接物让「点回原文」点到了一段谁也没写过的文字，而且**不报错**。
    """
    text = (
        "粗钢产量  CSPI月均\n"
        "数据来源：wind资讯\n"
        "公司把握国家供给侧结构改革、钢铁去产能的机遇。"
    )
    joined = join_wrapped_lines(text)
    assert "数据来源：wind资讯公司" not in joined, "说明行把正文吸进来了"

    # 正文必须**独立成句**——它进 claim 表时 source_text 才是年报里那一句
    sentences = [s.text for s in iter_sentences(text)]
    assert any(
        s.startswith("公司把握国家供给侧结构改革") for s in sentences
    ), f"正文没有独立成句：{sentences}"


def test_table_row_does_not_absorb_the_following_paragraph() -> None:
    """★ 回归：表格行是完整的块，后面的行是新段落。

    最初合并逻辑只看「上一行末尾有没有句号」，而表格行末尾本来就没有
    句号，于是被接上了正文——数字占比从 0.89 掉到 0.45，
    后面的表格剔除器再也认不出来，整串数字进了候选句。
    """
    text = "91919191919191919191919102020202020202020202020212121\n粗钢产量  CSPI月均\n数据来源：wind资讯2022年，公司统筹疫情防控。"
    joined = join_wrapped_lines(text)
    digit_line = next(line for line in joined.split("\n") if line.startswith("9191"))
    assert looks_like_table_row(digit_line), "表格行被后面的正文稀释了"

    sentences = iter_sentences(text)
    assert not any(s.text.startswith("9191") for s in sentences)


def test_wrapped_lines_are_joined_back() -> None:
    """PDF 会把句子硬换行，切句前必须接回去。

    「复苏的可能\\n性较大」不接的话，切出来的「句子」全是半截话，
    而它们照样能匹配主题词、照样进库、照样参与评分，**不报任何错**。
    """
    assert "复苏的可能性较大" in join_wrapped_lines("复苏的可能\n性较大")


def test_headings_are_dropped() -> None:
    text = "一、报告期内公司所处行业情况\n（一）主要业务\n公司主营业务为钢材产品的生产和销售。"
    texts = [s.text for s in iter_sentences(text)]
    assert not any(t.startswith("一、") for t in texts)
    assert any("主营业务" in t for t in texts)


# ---------------------------------------------------------------- 幅度


def test_magnitude_skips_the_year() -> None:
    """★ 回归：句子几乎都以「2022 年」开头，不能把它当成幅度。

    否则判定会拿「2022」去和实际值比，得到一个永远对不上的偏差，
    把好句子判成冲突——而且不报错。
    """
    m = extract_magnitude("2022年，公司销售商品坯材4,976.3万吨。")
    assert m is not None
    assert m.value == D("4976.3")
    assert m.unit == "万吨"


def test_magnitude_prefers_the_one_with_a_unit() -> None:
    m = extract_magnitude("2024年营业收入322,116百万元")
    assert m is not None and m.unit == "百万元"


@pytest.mark.parametrize(
    "text,expected_bound",
    [
        ("销量增长20%以上", "at_least"),
        ("增长不低于5%", "at_least"),
        ("下降不超过10%", "at_most"),
        ("增长约8%", "about"),
        ("增长8%", "exact"),
    ],
)
def test_magnitude_bound(text: str, expected_bound: str) -> None:
    m = extract_magnitude(text)
    assert m is not None and m.bound == expected_bound


def test_no_magnitude_without_a_number() -> None:
    """没有数字的方向性表述只做「方向级验证」，不套用幅度阈值。"""
    assert extract_magnitude("销量持续增长，市场份额稳步提升。") is None


#: 宝钢每年的「年度经营计划」，一字不改，来自年报原文。
#: 一句话里五个目标，主判据是 operating_cost，唯一正确的是**最后一个**。
_PLAN_SENTENCE = (
    "2018年，宝钢股份计划产铁4563万吨、产钢4737万吨、"
    "销售商品坯材4568万吨、营业总收入2786亿元、营业成本2420亿元。"
)


def test_multi_target_sentence_picks_the_one_matching_the_metric() -> None:
    """★ 一句多目标时，要取**主判据对应的**那个数，不是第一个带单位的。

    不传别名的话取到的是「4,563 万吨（产铁）」——拿铁产量去和营业成本比，
    量纲完全不同，算出来的偏差毫无意义，然后判成「未达成」。**不报错。**
    """
    m = extract_magnitude(_PLAN_SENTENCE, ("营业成本", "主营业务成本"))
    assert m is not None
    assert m.value == D("2420"), f"取到了 {m.raw}"
    assert m.unit == "亿元"


def test_without_aliases_it_falls_back_to_the_first_unit() -> None:
    """不传别名时退回旧行为——这个兜底本身也要测。

    只测「传了别名就对」是不够的：把选择逻辑整个删掉、永远返回第一个，
    那条断言照样过。
    """
    m = extract_magnitude(_PLAN_SENTENCE)
    assert m is not None and m.value == D("4563")


def test_plan_marker_is_recorded() -> None:
    """「计划」这两个字必须变成一个落库的信号。

    判定那一步手上只有 `claim` 表的这一行，回头去原文找「计划」是找不到的。
    """
    m = extract_magnitude(_PLAN_SENTENCE, ("营业成本",))
    assert m is not None and m.is_plan is True


def test_a_reported_fact_is_not_marked_as_a_plan() -> None:
    """上一条的反面——**更要紧的那一面**。

    拿事实去核验事实永远判「支持」，而假的「支持」看不出来。
    所以「有数字」不等于「是计划」。

    「、」不算分句边界也是有意的：「计划」在句首，管的是整个顿号列表，
    若把「、」也当边界，「营业成本2420亿元」那一段里就没有「计划」二字了。
    """
    m = extract_magnitude("2022年，公司销售商品坯材4,976.3万吨。")
    assert m is not None
    assert m.is_plan is False

    # 「，」算边界：计划管不到逗号后面的那一句
    m2 = extract_magnitude("公司计划提升产能，2022年实际销量4,976.3万吨。")
    assert m2 is not None and m2.is_plan is False


def test_a_year_range_is_not_a_magnitude() -> None:
    """「2019-2021年规划目标」里的 2019 既不是金额也不是数量。

    只判「数字后面紧跟着年字」会漏掉它——那里跟的是减号。
    取到之后幅度变成 2019，判定拿它和实际值比，得到一个永远对不上的偏差。
    """
    assert extract_magnitude("公司2019-2021年规划目标的收官之年") is None
    assert extract_magnitude("2019—2021年规划") is None


# ---------------------------------------------------------------- 方向


@pytest.mark.parametrize(
    "text,expected",
    [
        ("营业收入同比增长8%", "up"),
        ("营业收入同比下降8%", "down"),
        ("回款情况明显改善", "improve"),
        ("经营形势持续恶化", "deteriorate"),
        ("产销量基本持平", "flat"),
        ("公司召开了董事会", "unknown"),
    ],
)
def test_direction(text: str, expected: str) -> None:
    direction, _ = extract_direction(text)
    assert direction == expected


def test_negated_direction_is_not_taken_at_face_value() -> None:
    """「未能实现增长」按字面判会得到**完全相反**的结论，且不报错。"""
    direction, _ = extract_direction("本期未能实现销量增长")
    assert direction != "up"


def test_modality_only_is_flagged() -> None:
    """意向表述不是保证承诺。

    「力争提升」可以跟踪，但**不能据此推断虚假陈述**——所以这里把它
    标出来，由调用方降低置信度，而不是当成实打实的承诺。
    """
    _, modality = extract_direction("公司力争实现销量提升")
    assert modality is True
    _, plain = extract_direction("公司实现销量提升")
    assert plain is False


# ---------------------------------------------------------------- 期间


def test_forward_claim_targets_the_next_year() -> None:
    """★ 2023 年年报里说「2024 年…」，目标期间是 **2024**。

    搞反了会产生事后偏见的「处处支持」——拿已经发生的事实去「验证」
    与之同时写下的表述，当然处处对上。而且这个错误**不报任何错**，
    只会让指数虚高。docs/00 §八 把未来信息泄漏列为评测边界之一。
    """
    assert resolve_period("2024年公司计划实现销量增长", "2023") == "2024"
    assert resolve_period("公司预计明年销量提升", "2023") == "2024"
    assert resolve_period("公司本期销量提升", "2023") == "2023"
    assert resolve_period("较上年同期有所改善", "2023") == "2022"


def test_forward_mapping_can_be_disabled() -> None:
    """rule_config 的 narrative.forward_verifies_next_year 置 0 时不做下一年映射。"""
    assert resolve_period("公司预计明年销量提升", "2023", forward_verifies_next_year=False) is None


def test_a_quantity_is_not_a_year() -> None:
    """★ 句子里的四位数不等于它是年份。

    年份正则若写成 `(20\\d{2})\\s*年?`（「年」可选），它会匹配任何 20xx 开头的数。
    首钢 2023 年报原句「铁2147万吨……材2073万吨，同比降低6.5%」于是拿到
    `period_norm='2073'`——变成一条 **2073 年到期** 的前瞻计划：
    永远进不了验证、永远不判冲突，却实实在在占着 H 的观测集。

    实测这句在库里存了 3 行，`period_expr` 是「2023年」而 `period_norm` 是
    «2073»——同一个函数抽出来的两个字段自相矛盾，而页面上看不出哪个算数。
    所以两个都要断言。
    """
    text = "铁2147万吨，同比降低3.4%；钢2222万吨，同比降低4.3%;材2073万吨，同比降低6.5%。"
    assert resolve_period(text, "2023") is None, "数量词被当成了年份"
    assert extract_period_expr(text) is None, "期间原文里不该出现 2073"


def test_a_real_year_still_resolves() -> None:
    """上一条的反面：真正带「年」的年份照常认出来。

    只断言「2073 不再是年份」是不够的——把年份识别整个关掉也能让它通过。
    """
    assert resolve_period("2024年公司计划实现销量增长", "2023") == "2024"
    assert extract_period_expr("2024年公司计划实现销量增长") == "2024年"


def test_pending_targets_are_excluded_from_the_denominator() -> None:
    """未到期的计划不进覆盖率分母——它在逻辑上无法判定，
    算进去只会稀释覆盖率、把 n/N 压到闸门以下。"""
    known = ("2022", "2023", "2024")
    assert is_pending("2025", known) is True
    assert is_pending("2023", known) is False
    assert is_pending(None, known) is False


# ---------------------------------------------------------------- 主题


def test_theme_match() -> None:
    m = match_theme("公司加大降本控费挖潜力度")
    assert m is not None and m.rule.theme == "cost_reduction"


def test_unimplemented_themes_are_marked_not_dropped() -> None:
    """★ 匹配上了但没有直接指标可验证，**不等于没匹配上**。

    必须标 unverifiable 并写明原因，不能当成无主题丢弃——
    那会把「我们缺字段」伪装成「年报没提这件事」。
    """
    m = match_theme("公司持续推进产品结构优化，高端产品占比提升")
    assert m is not None
    assert m.usable is False
    assert "字段字典" in m.rule.unimplemented_reason or "未采集" in m.rule.unimplemented_reason


def test_every_theme_declares_its_forbidden_simplifications() -> None:
    """每类主题都要写明被禁止的简化推断。

    这些是会计口径 v1.1 §A.6 点名的：毛利率下降不等于降本失败、
    CFO 下滑不能单独否定回款……它们「看起来给出了结论」，
    而那个结论没有任何证据支撑。
    """
    for rule in THEMES:
        assert rule.forbidden, f"{rule.theme} 没写禁止的简化推断"
        if not rule.implementable:
            assert rule.unimplemented_reason, f"{rule.theme} 标了不可实现但没说原因"


def test_no_theme_match_returns_none() -> None:
    assert match_theme("公司召开了第八届董事会第十次会议。") is None


# ---------------------------------------------------------------- 数字与指标的对应


def test_a_number_next_to_the_metric_alias_is_aligned() -> None:
    """宝钢 2018 那一句：六个数字，靠「营业成本」这个别名取到 2,420 亿元。"""
    text = (
        "2018年，宝钢股份计划产铁4563万吨、产钢4737万吨、销售商品坯材4568万吨、"
        "营业总收入2786亿元、营业成本2420亿元。"
    )
    m = extract_magnitude(text, ("营业成本",))
    assert m is not None
    assert m.value == Decimal("2420")
    assert m.unit == "亿元"
    assert m.metric_aligned is True


def test_a_number_without_the_metric_alias_is_not_aligned() -> None:
    """★ 反面，也是这一位存在的理由。

    真实撞到过的原句：

        2024年，公司预算安排固定资产投资资金239.2亿元，主要用于……

    主题映射成 `operating_cost`，句子里的 239.2 亿元其实是**资本开支**。
    别名「营业成本」不在句子里，于是退回取第一个带单位的数——
    拿它和营业成本比，偏差 +280,626 百万元，按成本方向还是「未达成」。
    **有数字、有理由、有公式，和真结论长得一模一样。**

    所以「退回取数」这件事必须被记下来，一路传到判定层。
    """
    text = "2024年，公司预算安排固定资产投资资金239.2亿元，主要用于宝山基地项目。"
    m = extract_magnitude(text, ("营业成本",))
    assert m is not None
    assert m.value == Decimal("239.2"), "退回取的是第一个带单位的数"
    assert m.metric_aligned is False, "别名没命中，就必须标出来"


def test_no_aliases_supplied_is_not_a_mismatch() -> None:
    """★ **「没查」不等于「对不上」。**

    一个别名都没传时我们没有依据说它错位。报 False 会让一整类主张
    被静默降级成待核查——而调用方多半只是没拿到字典数据。
    """
    m = extract_magnitude("2024年公司实现营业收入3221亿元", ())
    assert m is not None
    assert m.metric_aligned is True


def test_the_alias_must_be_in_the_same_clause() -> None:
    """别名与数字之间跨分句就不算对齐——否则「营业成本同比下降，销量3000万吨」
    会拿 3000 万吨去当营业成本的目标。"""
    text = "公司营业成本同比下降，销量3000万吨。"
    m = extract_magnitude(text, ("营业成本",))
    assert m is not None
    assert m.metric_aligned is False
