"""数字守卫：拦住模型编造的数字。

为什么需要它
------------
本项目的分工是「**数字由程序计算，模型只负责理解和组织文字**」。但模型组织文字
时很爱顺手写一个数——可能是记错的、可能是从别处看来的、也可能是它自己算的。
这类数字**看起来完全合理**，混在正确的数字中间根本认不出来。

唯一能自动抓住的办法是：把这一轮所有工具返回的数字收集起来，逐字检查模型写的
每个数字能不能在里面找到。找不到就判为编造。

三个必须处理的现实问题
----------------------
1. **单位换算**。工具返回的是「百万元」，模型写的是「3221.16 亿元」——
   同一个数。直接字符串比对会把对的判成编造，那这个守卫很快就会被人关掉。
   所以这里把两边都归一到同一个基准单位再比。

2. **文本是四舍五入过的**。模型写「312.5 亿元」而精确值是 312.54 亿，
   这是**正常且正确的表述**。所以比对时把候选值按文本的小数位数四舍五入再比。

3. **年份不是金额**。「2024 年」里的 2024 不需要在工具结果里找得到。
   否则每一句话都会误报。

刻意不做的
----------
不判断数字的**含义**是否正确。守卫只能证明「这个数字来自本轮工具结果」，
不能证明「用在了对的地方」。引用错了字段名、张冠李戴，它抓不到——
那要靠证据链里的事实与来源页码给人复核。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable, Iterable, Sequence

# 各单位的换算系数，基准是「元」。
# ⚠ 顺序要紧：长单位必须排在短单位前面（万亿元 在 万元 之前，亿元 在 元 之前），
# 否则正则的或分支会先命中短的，把「亿元」拆成「亿」+ 没匹配上的「元」。
UNIT_FACTORS: tuple[tuple[str, Decimal], ...] = (
    ("万亿元", Decimal("1e12")),
    ("亿元", Decimal("1e8")),
    ("百万元", Decimal("1e6")),
    ("万元", Decimal("1e4")),
    ("千元", Decimal("1e3")),
    ("亿元", Decimal("1e8")),      # 重复一次是刻意的：保证它一定在「元」之前
    ("元", Decimal(1)),
    ("亿", Decimal("1e8")),
    ("万", Decimal("1e4")),
    ("%", Decimal("0.01")),
    ("％", Decimal("0.01")),
    ("个百分点", Decimal("0.01")),
    ("吨", Decimal(1)),
    ("天", Decimal(1)),
    ("倍", Decimal(1)),
    ("股", Decimal(1)),
)

_UNIT_PATTERN = "|".join(re.escape(u) for u, _ in UNIT_FACTORS)

# 千位分隔符、正负号（含中文减号 U+2212，年报里常见）、小数、可选单位
NUMBER_RE = re.compile(
    rf"""
    (?P<sign>[-−+])?
    (?P<num>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*(?P<unit>{_UNIT_PATTERN})?
    """,
    re.X,
)

# 4 位纯整数且没有单位，落在合理年份区间内 —— 当作年份，不参与比对
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

BASE_UNIT = "元"


@dataclass(frozen=True)
class NumberToken:
    """文本里发现的一个数字。"""

    raw: str
    value: Decimal                 # 归一化到基准单位之后的值
    unit: str | None
    start: int
    end: int
    context: str

    @property
    def decimals(self) -> int:
        """文本写了几个小数位。用于「四舍五入过的表述」的比对。"""
        if "." not in self.raw:
            return 0
        return len(self.raw.split(".", 1)[1])

    @property
    def display(self) -> str:
        """照原文的样子写回去，用于报错里指出是哪个数。"""
        return self.raw + (self.unit or "")


@dataclass(frozen=True)
class Violation:
    """一个找不到出处的数字。"""

    token: NumberToken
    reason: str

    @property
    def display(self) -> str:
        return self.token.display


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    checked: int                            # 检查了多少个数字
    violations: tuple[Violation, ...] = ()

    @property
    def fabricated(self) -> tuple[str, ...]:
        return tuple(v.display for v in self.violations)

    def describe(self) -> str:
        if self.ok:
            return f"数字守卫通过：{self.checked} 个数字都能在本轮工具结果里找到。"
        return (
            f"★ 数字守卫拦下 {len(self.violations)} 个找不到出处的数字"
            f"（共检查 {self.checked} 个）：{'、'.join(self.fabricated)}"
        )


# ---------------------------------------------------------------- 提取


def extract_numbers(
    text: str,
    *,
    context_chars: int = 24,
    ignore_years: bool = True,
) -> tuple[NumberToken, ...]:
    """抽出文本里所有带单位的数字。

    `ignore_years` 为真时跳过「2024 年」这类四位年份——不加这一条，
    几乎每句话都会误报，守卫会被人直接关掉。
    """
    tokens: list[NumberToken] = []
    for m in NUMBER_RE.finditer(text):
        raw = m.group("num")
        unit = m.group("unit")

        if ignore_years and unit is None and _YEAR_RE.match(raw):
            continue

        try:
            value = Decimal(raw.replace(",", ""))
        except InvalidOperation:      # pragma: no cover - 正则已保证是数字
            continue
        if m.group("sign") in ("-", "−"):
            value = -value

        factor = _factor_for(unit)
        tokens.append(
            NumberToken(
                raw=raw,
                value=value * factor,
                unit=unit,
                start=m.start(),
                end=m.end(),
                context=_context(text, m.start(), m.end(), context_chars),
            )
        )
    return tuple(tokens)


def _factor_for(unit: str | None) -> Decimal:
    if unit is None:
        return Decimal(1)
    for name, factor in UNIT_FACTORS:
        if name == unit:
            return factor
    return Decimal(1)      # pragma: no cover - 受 _UNIT_PATTERN 约束


def _context(text: str, start: int, end: int, width: int) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    return text[left:right].replace("\n", " ")


# ---------------------------------------------------------------- 比对


def check_numbers(
    text: str,
    allowed: Iterable[Decimal | str | int],
    *,
    base_unit: str = BASE_UNIT,
    extra_allowed: Iterable[Decimal | str | int] = (),
) -> GuardResult:
    """检查文本里每个数字是否都能在 `allowed` 里找到。

    `allowed` 里的值**必须已经换算到 `base_unit`**（默认「元」）。库里的金额
    存的是百万元，传进来之前乘 1e6——见 `million_to_yuan()`。

    `extra_allowed` 用来放不来自工具结果、但确实允许出现的数（比如口径参数、
    明确的常数）。**刻意分开**：混进 allowed 会让人分不清哪些数字是被验证过的。
    """
    candidates = _normalize_allowed((*allowed, *extra_allowed))
    tokens = extract_numbers(text)

    violations: list[Violation] = []
    for token in tokens:
        if not _matches(token, candidates):
            violations.append(
                Violation(
                    token=token,
                    reason=(
                        f"{token.display} 在本轮工具结果里找不到对应值"
                        f"（已按单位归一化到{base_unit}并允许四舍五入）"
                    ),
                )
            )

    return GuardResult(ok=not violations, checked=len(tokens), violations=tuple(violations))


def _normalize_allowed(values: Iterable[Decimal | str | int]) -> tuple[Decimal, ...]:
    out: list[Decimal] = []
    for v in values:
        if isinstance(v, Decimal):
            out.append(v)
        else:
            try:
                out.append(Decimal(str(v)))
            except InvalidOperation:
                continue
    return tuple(out)


def _matches(token: NumberToken, candidates: Sequence[Decimal]) -> bool:
    """文本里的数字能否由某个候选值四舍五入得到。

    精度必须**换算到归一化空间**再比，不能直接用原文的小数位：

        「5%」→ 归一化值 0.05，单位系数 0.01，原文 0 位小数
               → 归一化空间的精度是 0.01 × 10⁰ = 0.01，不是 1

        「3221.16 亿元」→ 单位系数 1e8，原文 2 位小数
               → 精度是 1e8 × 10⁻² = 1e6

    直接用原文小数位的话，百分比、亿元这些都要差几个数量级，**每个数字都对不上**。

    反过来也不能放宽成「误差在多少以内就算过」——那会让 312.5 匹配到 313，
    就不叫验证出处了。按文本自身声明的精度四舍五入，是唯一说得通的口径：
    文本写了几位，就只对那几位负责。
    """
    quantum = _factor_for(token.unit) * Decimal(1).scaleb(-token.decimals)
    for candidate in candidates:
        try:
            rounded = candidate.quantize(quantum, rounding=ROUND_HALF_UP)
        except InvalidOperation:      # pragma: no cover - 超出 Decimal 精度时
            continue
        if rounded == token.value:
            return True
    return False


def million_to_yuan(value: Decimal | str | int) -> Decimal:
    """把「百万元」换算成「元」。

    库里金额统一存百万元，而模型和年报原文说的是元 / 万元 / 亿元。
    守卫的默认基准是元，所以调用方要先用它把事实值换算过去。
    """
    return Decimal(str(value)) * Decimal("1e6")


# ---------------------------------------------------------------- 拦截改写


def redact(
    text: str,
    result: GuardResult,
    *,
    marker: str = "【数字未经核实，已移除】",
) -> str:
    """把查不到出处的数字替换掉，保留其余文字。

    替换而不是丢弃整段：模型的**文字组织**部分往往是对的，问题只在那几个数上。
    整段丢掉会把有用的话一起丢掉，而这段话是要给人看的。
    """
    if result.ok:
        return text
    spans = sorted(
        ((v.token.start, v.token.end) for v in result.violations), reverse=True
    )
    out = text
    for start, end in spans:
        out = out[:start] + marker + out[end:]
    return out


def guard_or_redact(
    text: str,
    allowed: Iterable[Decimal | str | int],
    *,
    on_violation: Callable[[GuardResult], None] | None = None,
) -> tuple[str, GuardResult]:
    """常用组合：检查一遍，有问题就改写并回调记录。

    回调里应当写日志——被拦下的数字是**模型质量问题的一手证据**，
    不记下来就没法回答「这个模型在我们这个场景上到底靠不靠谱」。
    """
    result = check_numbers(text, allowed)
    if not result.ok and on_violation is not None:
        on_violation(result)
    return redact(text, result), result
