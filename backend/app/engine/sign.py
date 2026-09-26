"""资产减值损失的符号标准化（会计口径 v1.1 §二）。

纯函数，零 IO。

## 问题

同一家公司的「资产减值损失」在不同年份的**列报方向相反**：

    2014–2018   正数列示  +1,486,729,666.32   ← 旧式：损失为正
    2021–2024   负数列示    -575,389,035.63   ← 新式：「损失以负号填列」

直接拿去算跨年趋势，差值会是「两个同号数相加」而不是「相减」，
**趋势直接反向**，而且不报任何错。

## 口径

`sign_convention = loss_positive`——**损失额为正**。只改变系统的标准化表示，
不改变原始报表（`value_raw` 永远留着原样）。

    正数代表损失的旧式列报  →  normalized = raw
    明示「损失以负号填列」  →  normalized = -raw
    对利润的影响            →  profit_impact = -normalized

## 三条容易踩的边界

1. **禁止使用绝对值。** 允许转回的项目在新版列报里为正时，
   标准化应为**负**（表示净收益）。取绝对值会把「转回收益」也变成「损失」，
   方向正好相反。
2. **不能凭年份硬编码。** 以该表表头、行注及利润勾稽确定。
   定不下来时返回 `needs_review`，不猜。
3. **「资产减值损失」是期间损益，「减值准备」是余额，不能互相替代。**

另外：**标准化符号统一 ≠ 跨期经济可比**。新金融工具准则前后的
资产减值 / 信用减值分类不同，还须另作重分类桥接——那是另一件事，
本模块不做，也不能声称做了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

SignBasis = Literal["loss_positive", "loss_negative", "not_applicable"]

LOSS_POSITIVE: SignBasis = "loss_positive"
LOSS_NEGATIVE: SignBasis = "loss_negative"
NOT_APPLICABLE: SignBasis = "not_applicable"

#: 连接符的各种写法。年报里混用 ASCII 连字符、数学减号、全角连字符——
#: 少列一种就会把那一年的符号判反，而且不影响任何校验。
_DASH = r"[-−—–－﹣]"

#: 明细表头/行注里说明「损失以负号填列」的说法。年报里写法不统一，
#: 列全一些——漏一种就会把那一年的符号判反，而且不影响任何校验。
_LOSS_AS_NEGATIVE_PATTERNS = (
    rf"损失以[「\"'“‘]?{_DASH}[」\"'”’]?号?填列",
    r"损失以负号填列",
    r"损失以[「\"'“‘]?减号",
    rf"以[「\"'“‘]?{_DASH}[」\"'”’]?号填列",
    r"损失列示为负",
)

#: 明确说明「正数为损失」的说法
_LOSS_AS_POSITIVE_PATTERNS = (
    r"损失以正数",
    r"正数表示损失",
    r"损失额为正",
)

#: 这些词出现说明这一行**不是期间损益**，而是准备余额。
#:
#: ⚠ 匹配的是**文本**不是 metric_key：metric_key 是英文的
#: （`goodwill_impairment`），拿中文词去比永远比不上——
#: 那个检查会静默失效，一个都拦不住。
_NOT_IMPAIRMENT_HINTS = ("减值准备", "坏账准备", "跌价准备", "减值准备期末余额")


@dataclass(frozen=True)
class SignDecision:
    """一次符号依据的判定。"""

    basis: SignBasis
    #: 判定依据的原文片段，**必须留**。人工复核时要能看出系统凭什么这么判。
    evidence: str | None
    reason: str

    @property
    def determined(self) -> bool:
        """能不能确定。

        False 时调用方应当把该行的 status 落 `needs_review`，
        **不猜一个符号**——猜错的后果是趋势反向，而且不报错。
        """
        return self.basis != NOT_APPLICABLE


UNDETERMINED = SignDecision(
    basis=NOT_APPLICABLE,
    evidence=None,
    reason=(
        "表头、行注与勾稽都不足以确定列报符号。按 v1.1 落 needs_review，"
        "**不猜**——猜错的后果是跨年趋势反向，而且不报错。"
    ),
)


def decide_sign_basis(
    *,
    row_text: str | None = None,
    header_text: str | None = None,
    metric_key: str = "",
) -> SignDecision:
    """从表头与行注判断列报符号。

    ⚠ **不能凭年份判。** 「2015 年之后都是负号」看着像规律，但只要有
    一家公司在某一年改回正数列示，那条规则就会静默地把符号判反——
    而它不会影响任何校验，只会让趋势图反向。
    """
    haystack = " ".join(t for t in (row_text, header_text) if t)
    if not haystack.strip():
        return UNDETERMINED

    # ⚠ 在**文本**里找「减值准备」，不是 metric_key 里——
    # metric_key 是英文的，拿中文词去比永远比不上。
    if any(h in haystack for h in _NOT_IMPAIRMENT_HINTS):
        return SignDecision(
            basis=NOT_APPLICABLE,
            evidence=None,
            reason="这一行是减值准备余额，不是期间损益，与符号标准化无关。",
        )

    for pattern in _LOSS_AS_NEGATIVE_PATTERNS:
        m = re.search(pattern, haystack)
        if m:
            return SignDecision(
                basis=LOSS_NEGATIVE,
                evidence=_snippet(haystack, m.start(), m.end()),
                reason=f"表头或行注明示「{m.group(0)}」，损失以负号填列。",
            )

    for pattern in _LOSS_AS_POSITIVE_PATTERNS:
        m = re.search(pattern, haystack)
        if m:
            return SignDecision(
                basis=LOSS_POSITIVE,
                evidence=_snippet(haystack, m.start(), m.end()),
                reason=f"表头或行注明示「{m.group(0)}」，损失为正数。",
            )

    return UNDETERMINED


def _snippet(text: str, start: int, end: int, width: int = 16) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    return text[left:right].replace("\n", " ")


def normalize_impairment(value: Decimal, basis: SignBasis) -> Decimal:
    """按符号依据把数值标准化为「损失为正」。

    ⚠ **这个函数只翻符号，不换算单位。** 传进来的值是什么单位，
    出来的就是什么单位——符号翻转本身与量纲无关。

    实测踩过：拿**元**的原始值去掉符号、再写进以**百万元**存储的列，
    整列数据被放大 100 万倍。而且符号看起来是对的，
    扫一眼数值也「像是那么大个数」——只有对着原始披露核一遍才发现。
    所以：**作用在已经换算好的值上**（`value_millions`），不是 `value_raw`。

    ⚠ **不用绝对值。** 允许转回的项目在新版列报里为正时，
    标准化应为负（表示净收益）——取绝对值会把转回收益也变成损失，
    方向正好相反，而且不报错。
    """
    if basis == LOSS_NEGATIVE:
        return -value
    # loss_positive 与 not_applicable 都原样返回：
    # 前者本来就是标准形式；后者不确定，**改动它才是猜**
    return value


def profit_impact(normalized: Decimal) -> Decimal:
    """标准化后的减值对利润的影响。

    `profit_impact = -normalized`——损失越大，对利润的负面影响越大。
    """
    return -normalized


def is_reversal(normalized: Decimal) -> bool:
    """标准化后为负，说明是**转回或净收益**，不是损失。

    这一条值得单独判：长期资产减值按适用准则一般不得转回，
    出现负数要先核查而不是直接当成合法转回。
    """
    return normalized < 0


# ---------------------------------------------------------------- 校验用例

#: 会计口径 v1.1 §二 给出的两个实例。实现必须与它们逐位一致。
ACCEPTANCE_CASES: tuple[tuple[str, str, str], ...] = (
    # (说明, 原始值, 期望的标准化值)
    ("2015 年，正数列示损失", "1486729666.32", "1486729666.32"),
    ("2024 年，「损失以负号填列」", "-578764052.76", "578764052.76"),
)


def check_acceptance_cases() -> list[str]:
    """跑一遍 v1.1 的两个实例，返回不一致的说明（空列表表示全对）。

    做成函数而不是测试，是为了让回填脚本也能在动手前自检一遍——
    标准化写错的话，回填会把整列数据改错，而那是不可逆的。
    """
    problems: list[str] = []
    for label, raw, expected in ACCEPTANCE_CASES:
        basis = LOSS_POSITIVE if not raw.startswith("-") else LOSS_NEGATIVE
        got = normalize_impairment(Decimal(raw), basis)
        if got != Decimal(expected):
            problems.append(f"{label}：期望 {expected}，得到 {got}")
    return problems
