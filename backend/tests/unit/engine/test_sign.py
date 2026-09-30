"""资产减值符号标准化的测试。

会计口径 v1.1 §二 给了两个实例，必须**逐位一致**：

    2015 年，正数列示损失        1,486,729,666.32 → 1,486,729,666.32（不变）
    2024 年，「损失以负号填列」  −578,764,052.76 → 578,764,052.76（取反）

这两个数不是编的，是会计同学从宝钢年报里摘出来交付的。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.sign import (
    ACCEPTANCE_CASES,
    LOSS_NEGATIVE,
    LOSS_POSITIVE,
    NOT_APPLICABLE,
    check_acceptance_cases,
    decide_sign_basis,
    is_reversal,
    normalize_impairment,
    profit_impact,
)

D = Decimal


# ---------------------------------------------------------------- 验收实例


def test_v11_acceptance_cases_pass() -> None:
    """★ v1.1 §二 的两个实例逐位一致。"""
    assert check_acceptance_cases() == []


@pytest.mark.parametrize("label,raw,expected", ACCEPTANCE_CASES)
def test_v11_case_by_case(label: str, raw: str, expected: str) -> None:
    basis = LOSS_POSITIVE if not raw.startswith("-") else LOSS_NEGATIVE
    assert normalize_impairment(D(raw), basis) == D(expected), label


def test_positive_loss_is_unchanged() -> None:
    assert normalize_impairment(D("1486729666.32"), LOSS_POSITIVE) == D("1486729666.32")


def test_negative_disclosure_is_negated() -> None:
    assert normalize_impairment(D("-578764052.76"), LOSS_NEGATIVE) == D("578764052.76")


# ---------------------------------------------------------------- 禁止绝对值


def test_reversal_stays_negative() -> None:
    """★ **禁止使用绝对值。**

    允许转回的项目在新版列报里为正时，标准化应为**负**，表示净收益。
    取绝对值会把「转回收益」也变成「损失」——方向正好相反，而且不报错。
    """
    # 新版列报里 +100 表示转回收益 → 标准化为 -100（净收益）
    assert normalize_impairment(D("100"), LOSS_NEGATIVE) == D("-100")
    assert is_reversal(D("-100")) is True


def test_reversal_is_detectable() -> None:
    """标准化后为负说明是转回。

    长期资产减值按适用准则一般不得转回——出现负数要先核查，
    而不是直接当成合法转回。
    """
    assert is_reversal(D("-1")) is True
    assert is_reversal(D("1")) is False
    assert is_reversal(D("0")) is False


def test_profit_impact_is_the_negation() -> None:
    """对利润的影响 = −标准化值。损失越大，负面影响越大。"""
    assert profit_impact(D("1486729666.32")) == D("-1486729666.32")
    assert profit_impact(D("-100")) == D("100")   # 转回增加利润


# ---------------------------------------------------------------- 符号判定


@pytest.mark.parametrize(
    "text",
    [
        "损失以负号填列",
        "损失以“-”号填列",
        "损失以-号填列",
        "资产减值损失（损失以“－”号填列）",
        "损失列示为负",
    ],
)
def test_negative_disclosure_phrasings_are_recognised(text: str) -> None:
    """年报里「损失以负号填列」的写法不统一。

    **漏一种就会把那一年的符号判反**，而且不影响任何校验——
    只会让跨年趋势反向。
    """
    d = decide_sign_basis(header_text=text, metric_key="impairment_loss")
    assert d.basis == LOSS_NEGATIVE, f"没认出：{text}"
    assert d.evidence, "判定必须留下原文依据"


def test_positive_disclosure_is_recognised() -> None:
    d = decide_sign_basis(header_text="损失以正数填列", metric_key="impairment_loss")
    assert d.basis == LOSS_POSITIVE


def test_undetermined_is_not_a_guess() -> None:
    """★ 判不出来就返回未确定，**不猜**。

    猜错的后果是跨年趋势反向，而且不报错。
    """
    d = decide_sign_basis(header_text="资产减值损失", metric_key="impairment_loss")
    assert d.determined is False
    assert d.basis == NOT_APPLICABLE
    assert "不猜" in d.reason


def test_empty_evidence_is_undetermined() -> None:
    assert decide_sign_basis(metric_key="impairment_loss").determined is False


def test_preparation_balance_is_not_applicable() -> None:
    """「减值准备」是余额，「减值损失」是期间损益——两者不能互相替代。"""
    d = decide_sign_basis(
        row_text="商誉减值准备", header_text="减值准备", metric_key="goodwill_impairment"
    )
    assert d.basis == NOT_APPLICABLE
    assert "余额" in d.reason


def test_preparation_check_looks_at_the_text_not_the_metric_key() -> None:
    """★ 「减值准备」要在**文本**里找，不能在 metric_key 里找。

    metric_key 是英文的（`goodwill_impairment`），拿中文词去比永远比不上——
    那个检查会静默失效，一个都拦不住，而代码看起来是「有防护的」。
    """
    import inspect

    from app.engine import sign

    src = inspect.getsource(sign.decide_sign_basis)
    assert "_NOT_IMPAIRMENT_HINTS" in src
    # 提示词必须作用在 haystack（文本）上，不是 metric_key 上
    assert "any(h in haystack for h in _NOT_IMPAIRMENT_HINTS)" in src


def test_not_applicable_does_not_change_the_value() -> None:
    """依据未定时**原样返回**——改动它才是猜。"""
    assert normalize_impairment(D("100"), NOT_APPLICABLE) == D("100")


# ---------------------------------------------------------------- 硬编码防线


def test_decision_does_not_depend_on_the_year() -> None:
    """★ **不能凭年份硬编码符号。**

    「2015 年之后都是负号」看着像规律，但只要有公司在某一年改回正数列示，
    那条规则就会静默地把符号判反。判定只能看表头与行注。
    """
    import ast
    import inspect

    from app.engine import sign

    tree = ast.parse(inspect.getsource(sign.decide_sign_basis))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    # 去掉文档字符串再看——文档里举例提到年份是正常的，
    # 要看的是**实际代码**里有没有按年份分支
    body = [n for n in func.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    code = "\n".join(ast.unparse(n) for n in body)
    for year in ("2015", "2024", "2014", "2018"):
        assert year not in code, f"判定代码里出现了年份 {year}"


def test_decision_carries_evidence_for_review() -> None:
    """判定必须留下**原文依据**——人工复核时要看得出系统凭什么这么判。"""
    d = decide_sign_basis(header_text="（损失以“-”号填列）", metric_key="impairment_loss")
    assert d.evidence and "-" in d.evidence
    assert d.reason
