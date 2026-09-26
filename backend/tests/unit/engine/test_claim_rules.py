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
