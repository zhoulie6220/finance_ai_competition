"""数字守卫的测试。

守卫存在的意义是抓**编造的数字**。但它的三个主要风险都是「误报」——
单位不同、四舍五入、年份——误报多了人就会把它关掉，那还不如没有。
所以下面误报方向的用例和抓漏方向的用例一样多。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.agents.guards import (
    check_numbers,
    extract_numbers,
    guard_or_redact,
    million_to_yuan,
    redact,
)

D = Decimal


# ---------------------------------------------------------------- 抓编造


def test_fabricated_number_is_caught():
    """★ 核心功能：工具结果里没有的数字必须被拦下。

    真实场景：工具只返回了营业收入 322116 百万元，模型却写了「净利润 8,900 百万元」。
    """
    text = "本年度营业收入 3221.16 亿元，净利润 8900 百万元。"
    r = check_numbers(text, [million_to_yuan("322116")])
    assert r.ok is False
    assert "8900" in r.fabricated[0]
    assert "3221.16" not in r.fabricated


def test_matching_number_passes():
    text = "本年度营业收入 3221.16 亿元。"
    r = check_numbers(text, [million_to_yuan("322116")])
    assert r.ok is True
    assert r.checked == 1


# ---------------------------------------------------------------- 误报防线


def test_unit_is_normalized_before_comparing():
    """★ 单位换算：工具给「百万元」，模型写「亿元」——同一个数。

    不做换算的话，几乎每次正常输出都会被拦下，守卫必然被关掉。
    """
    # 322116 百万元 = 3221.16 亿元 = 32211600 万元
    allowed = [million_to_yuan("322116")]
    for text, ok in [
        ("营业收入 322,116 百万元", True),
        ("营业收入 3221.16 亿元", True),
        ("营业收入 32211600 万元", True),
        ("营业收入 322116000000 元", True),
        ("营业收入 322116000 万元", False),      # 少一位，应拦下
    ]:
        assert check_numbers(text, allowed).ok is ok, text


def test_rounded_text_matches_the_exact_value():
    """★ 文本是四舍五入过的表述，这不是编造。

    精确值 312.54 亿元，写成「312.5 亿元」完全正常。按文本的小数位数把候选值
    四舍五入再比——而不是放宽误差范围。后者会让 312.5 匹配上 313，
    那就不叫验证出处了。
    """
    exact = million_to_yuan("31254")           # 31254 百万元 = 312.54 亿元
    assert check_numbers("约 312.5 亿元", [exact]).ok is True
    assert check_numbers("约 313 亿元", [exact]).ok is True    # 312.54 舍入到整数就是 313
    assert check_numbers("约 320 亿元", [exact]).ok is False   # 这个才是编造


def test_years_are_not_treated_as_amounts():
    """「2024 年」里的 2024 不需要在工具结果里找得到。

    不加这一条，几乎每句话都会误报。
    """
    r = check_numbers("2024 年公司实现营业收入 3221.16 亿元。", [million_to_yuan("322116")])
    assert r.ok is True
    assert r.checked == 1        # 只检查了金额，年份没算


def test_four_digit_amount_with_unit_is_still_checked():
    """带单位的四位数仍然是金额，不能被当成年份放过去。"""
    r = check_numbers("产量 2024 万吨", [million_to_yuan("1")])
    assert r.ok is False
    assert r.checked == 1


def test_percent_is_normalized_to_a_ratio():
    """「增长 5%」对应比率 0.05——库里的比例就是这么存的。

    混用会让比例类的判定静默宽出 100 倍。
    """
    assert check_numbers("收入增长 5%", [D("0.05")]).ok is True
    assert check_numbers("收入增长 5%", [D("5")]).ok is False


def test_extra_allowed_is_separate_from_tool_results():
    """口径参数不该混进「被验证过的数字」里。

    分开传是为了让人一眼看清哪些数字来自工具结果、哪些是允许的常数。
    """
    text = "按 0.5% 的容差，营业收入 100 亿元。"
    r = check_numbers(text, [million_to_yuan("10000")], extra_allowed=[D("0.005")])
    assert r.ok is True
    assert r.checked == 2


# ---------------------------------------------------------------- 提取与改写


def test_extract_handles_signs_and_separators():
    tokens = extract_numbers("-1,234.56 万元和 −789 元")
    assert [t.value for t in tokens] == [D("-12345600"), D("-789")]


def test_redact_removes_only_the_fabricated_numbers():
    """只替换数字，保留文字。

    整段丢掉会把模型组织得挺好的话一起丢掉，而那段话是要给人看的。
    """
    text = "营业收入 3221.16 亿元，净利润 8900 百万元。"
    r = check_numbers(text, [million_to_yuan("322116")])
    out = redact(text, r)
    assert "3221.16" in out            # 有出处的保留
    assert "8900" not in out           # 编造的移除
    assert "营业收入" in out            # 文字保留


def test_redact_is_identity_when_guard_passes():
    text = "营业收入 100 亿元。"
    r = check_numbers(text, [million_to_yuan("10000")])
    assert redact(text, r) == text


def test_guard_or_redact_reports_violations():
    seen = []
    out, result = guard_or_redact(
        "净利润 8900 百万元。", [million_to_yuan("1")], on_violation=seen.append
    )
    assert len(seen) == 1
    assert not result.ok
    assert "8900" not in out


# ---------------------------------------------------------------- 边界


def test_text_without_numbers_is_vacuously_ok():
    r = check_numbers("公司经营情况总体平稳。", [])
    assert r.ok is True
    assert r.checked == 0


def test_million_to_yuan_is_exact():
    """换算必须用 Decimal——用 float 的话 322116×1e6 会带上浮点误差。"""
    assert million_to_yuan("322116") == D("322116000000")


@pytest.mark.parametrize(
    "text,expected_units",
    [
        ("1 万亿元", ["万亿元"]),
        ("1 百万元", ["百万元"]),
        ("1 万元", ["万元"]),
        ("1 元", ["元"]),
    ],
)
def test_unit_alternation_picks_the_longest_match(text, expected_units):
    """长单位必须排在短单位之前。

    「万亿元」若被拆成「万」+ 剩下的「亿元」，金额会差 1 亿倍，
    而且**不会报任何错**——只会让每个数字都对不上。
    """
    assert [t.unit for t in extract_numbers(text)] == expected_units
