"""周期正常化：能源钢铁行业 DCF 的估值起点。

背景
----
周期股不能用永续增长 DCF 直接从最近一年的 FCFF 外推。在周期高点上，FCFF 会因为
产品价格暴涨而虚高，直接永续增长等于把周期顶部当成常态，估值会离谱地乐观；
周期底部则反之。「正常化」就是用**穿越一个完整周期的中枢盈利能力**替代单年数据，
作为 DCF 预测的起点。

口径（见 docs/03-valuation-rules.md 与 rule_config 初值）
--------------------------------------------------------
窗口   主窗口 8 个完整财务年度，锚定在最新完整年度上滚动；可比年度不足 7 个时
       扩展到 10 年窗口重试；仍不足 8 个可比年度则返回 NORMALIZATION_INSUFFICIENT_DATA。
核心   收入加权的周期中位 EBIT margin。这是 DCF 的**唯一**起点。
覆盖   窗口必须同时包含显著高于与显著低于自身中枢的年份，否则判为 incomplete_cycle。
       目的是拦住「只经历过上行的窗口」——这种窗口算出来的中枢仍然是高点。

设计约束
--------
* **纯函数**：无 IO、无 datetime.now()、无 random、无全局状态。同输入必同输出。
* **Decimal**：金额与比率一律用 Decimal，禁止 float。结果以字符串出库。
* **拒绝优于猜测**：任何一步不满足条件就返回明确的失败状态码，绝不返回一个
  「带警告的数字」——下游 DCF 拿不到值就只能拒绝计算，这是刻意的。
* **可复算**：每个返回值都带 formula 与逐年明细，供证据链回溯。

状态码
------
normalized                  主窗口（8 年）成功
extended_normalized         回退窗口（10 年）成功
NORMALIZATION_INSUFFICIENT_DATA   可比年度不足，拒绝输出中枢值
incomplete_cycle            周期覆盖不完整，拒绝输出中枢值
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal, Sequence

# ---------------------------------------------------------------- 状态码

NORMALIZED = "normalized"
EXTENDED_NORMALIZED = "extended_normalized"
NORMALIZATION_INSUFFICIENT_DATA = "NORMALIZATION_INSUFFICIENT_DATA"
INCOMPLETE_CYCLE = "incomplete_cycle"

Status = Literal[
    "normalized",
    "extended_normalized",
    "NORMALIZATION_INSUFFICIENT_DATA",
    "incomplete_cycle",
]

EBIT_VARIANTS = ("reported", "adjusted")

# 公式文本集中在此，避免同一句公式在成功路径与失败路径里写成两份、日后改漏一处。
EBIT_FORMULAS = {
    "reported": "利润总额 + 利息费用 − 利息收入",
    "adjusted": "（利润总额 + 利息费用 − 利息收入）+ 经会计批准的调整项",
}
CROSSCHECK_FORMULA = "营业利润 + 财务费用"

# 计算结果保留的小数位。比率用 6 位足够，再多没有财务意义。
RATIO_PLACES = Decimal("0.000001")
MONEY_PLACES = Decimal("0.000001")


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class YearInput:
    """单个财务年度的输入。轻解析阶段只需要前四个字段。"""

    year: str
    revenue: Decimal
    total_profit: Decimal
    interest_expense: Decimal
    interest_income: Decimal
    # 交叉核对用（年报齐备时才有）
    operating_profit: Decimal | None = None
    financial_expense: Decimal | None = None
    # 可比性。系统只做初判，是否排除由会计同学确认。
    comparable: bool = True
    incomparable_reason: str | None = None
    # 调整项合计（已带符号：加回为正、剔除为负）与是否已获会计批准
    adjustment_delta: Decimal = Decimal(0)
    adjustment_approved: bool = False


@dataclass(frozen=True)
class NormalizationConfig:
    """口径参数。初值与 rule_config 保持一致，可从库里读出后传入。"""

    preferred_years: int = 8
    fallback_years: int = 10
    min_comparable_years: int = 7
    min_comparable_years_fallback: int = 8
    # 显著偏离中枢的幅度门槛（绝对百分点）。低于此值的年份既不算高盈利年，
    # 也不算低盈利年——这是覆盖校验能拦住「平淡窗口」的关键。
    min_cycle_amplitude: Decimal = Decimal("0.02")
    # 两种 EBIT 算法的允许差异
    crosscheck_tolerance: Decimal = Decimal("0.05")
    default_ebit_variant: Literal["reported", "adjusted"] = "reported"

    def __post_init__(self) -> None:
        if self.preferred_years <= 0 or self.fallback_years <= 0:
            raise ValueError("窗口年数必须为正")
        if self.fallback_years < self.preferred_years:
            raise ValueError("回退窗口不能小于主窗口，否则扩展没有意义")
        if self.min_comparable_years <= 0:
            raise ValueError("最少可比年度必须为正")


@dataclass(frozen=True)
class YearResult:
    """单个年度的计算明细，对应 normalization_year 表。"""

    year: str
    revenue: Decimal
    reported_ebit: Decimal
    crosscheck_ebit: Decimal | None
    adjusted_ebit: Decimal
    crosscheck_deviation: Decimal | None
    ebit_used: Decimal
    ebit_variant_used: Literal["reported", "adjusted"]
    ebit_margin: Decimal | None
    revenue_weight: Decimal | None
    phase: Literal["high", "normal", "low"] | None
    comparable: bool
    incomparable_reason: str | None
    included_in_median: bool


@dataclass(frozen=True)
class CoverageResult:
    passed: bool
    covers_high: bool
    covers_low: bool
    high_threshold: Decimal | None
    low_threshold: Decimal | None
    median_margin: Decimal | None
    amplitude: Decimal | None
    high_years: tuple[str, ...]
    low_years: tuple[str, ...]
    reason: str | None


@dataclass(frozen=True)
class NormalizationResult:
    status: Status
    window_mode: Literal["primary_8y", "fallback_10y"] | None
    window_start: str | None
    window_end: str | None
    years_available: int
    comparable_years: int
    coverage: CoverageResult | None
    ebit_margin_mid: Decimal | None
    ebit_margin_p25: Decimal | None
    ebit_margin_p75: Decimal | None
    years: tuple[YearResult, ...]
    ebit_variant: Literal["reported", "adjusted"]
    ebit_formula: str
    crosscheck_formula: str | None
    crosscheck_max_deviation: Decimal | None
    crosscheck_needs_review: bool
    insufficient_reason: str | None
    formula: str
    method_version: str = "normalization:v1"

    @property
    def ok(self) -> bool:
        """是否成功产出中枢值。下游 DCF 必须先检查这个。"""
        return self.status in (NORMALIZED, EXTENDED_NORMALIZED)


@dataclass(frozen=True)
class CrosscheckItem:
    """交叉验证项。**不影响 DCF**，只用于回答「结论稳不稳」。"""

    metric_key: str
    mid_cycle_value: Decimal | None
    unit: str | None
    variant: str = "primary"
    data_source: str = "self_calculated"
    basis: str | None = None
    deviation_note: str | None = None

    def agrees_with(self, other: Decimal, tolerance: Decimal) -> bool | None:
        """与另一口径的中枢值比较。任一方缺失则返回 None（无法判断）。"""
        if self.mid_cycle_value is None or other == 0:
            return None
        return abs(self.mid_cycle_value - other) / abs(other) <= tolerance


# ---------------------------------------------------------------- 纯函数


def reported_ebit(y: YearInput) -> Decimal:
    """Reported EBIT = 利润总额 + 利息费用 − 利息收入。

    这是主 DCF 的默认口径，也是历史轻解析阶段唯一能算出来的口径。
    """
    return y.total_profit + y.interest_expense - y.interest_income


def crosscheck_ebit(y: YearInput) -> Decimal | None:
    """Cross-check EBIT = 营业利润 + 财务费用。

    仅在年报数据齐备时可用。注意它包含投资收益等非经营项，与 Reported EBIT
    天然存在口径差，因此只用作**核对**，不直接参与估值。
    """
    if y.operating_profit is None or y.financial_expense is None:
        return None
    return y.operating_profit + y.financial_expense


def adjusted_ebit(y: YearInput) -> Decimal:
    """Adjusted EBIT = Reported EBIT + 调整合计。

    调整明细见 ebit_adjustment 表。**未获会计批准前不得用于主 DCF**。
    """
    return reported_ebit(y) + y.adjustment_delta


def ebit_margin(ebit: Decimal, revenue: Decimal) -> Decimal | None:
    """EBIT margin。营业收入为零或负时不计算——返回 None 而非 0，
    避免把「算不出来」混同于「利润率为零」。"""
    if revenue <= 0:
        return None
    return (ebit / revenue).quantize(RATIO_PLACES)


def crosscheck_deviation(y: YearInput) -> Decimal | None:
    """两种 EBIT 算法的相对差异。超过容差时进入人工复核。"""
    r = reported_ebit(y)
    c = crosscheck_ebit(y)
    if c is None or r == 0:
        return None
    return (abs(c - r) / abs(r)).quantize(RATIO_PLACES)


def quantile(sorted_values: Sequence[Decimal], q: Decimal) -> Decimal | None:
    """线性插值分位数。sorted_values 必须已升序。"""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    pos = q * Decimal(n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - Decimal(lo)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def revenue_weighted_quantile(
    pairs: Sequence[tuple[Decimal, Decimal]], q: Decimal
) -> Decimal | None:
    """收入加权分位数：按数值升序累加权重，累计权重首次达到 q×总权重时对应的数值。

    为什么不用简单算术平均：周期里小年度的利润率会被异常放大（分母小），
    算术平均会让这些年份获得与其经济权重不相称的影响。若某年收入占窗口九成，
    它的利润率就应当主导中枢——这正是「收入加权」的意义所在。
    """
    usable = [(v, w) for v, w in pairs if w > 0]
    if not usable:
        return None
    usable.sort(key=lambda p: p[0])
    total = sum((w for _, w in usable), Decimal(0))
    target = total * q
    cum = Decimal(0)
    for value, weight in usable:
        cum += weight
        if cum >= target:
            return value
    return usable[-1][0]


def revenue_weighted_median(pairs: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    """收入加权中位数，即 q = 0.5 的加权分位数。"""
    return revenue_weighted_quantile(pairs, Decimal("0.5"))


def check_cycle_coverage(
    year_margins: Sequence[tuple[str, Decimal, Decimal]],
    cfg: NormalizationConfig,
) -> CoverageResult:
    """高低盈利阶段覆盖校验。

    参数 `year_margins` 为 (年份, EBIT margin, 营业收入) 三元组序列——带收入是为了
    让中枢与 normalize() 报告的中枢**用同一种加权方式**。若这里用未加权中位数、
    而报告值用收入加权中位数，两者的口径就不一致了。

    判定：以窗口自身的中枢为基准，高于中枢 `min_cycle_amplitude` 的年份算高盈利年，
    低于同等幅度的算低盈利年，两者都必须至少各有一个。

    为什么不用分位数判定：对任何给定窗口，分位数总能找到「最高的那个」和「最低的
    那个」，校验恒为真，拦不住任何东西。用「相对中枢的绝对落差」才能拦住「全是平淡
    年份」的窗口——所有年份都挤在中枢附近时，两个条件同时不成立。
    """
    if not year_margins:
        return CoverageResult(
            passed=False,
            covers_high=False,
            covers_low=False,
            high_threshold=None,
            low_threshold=None,
            median_margin=None,
            amplitude=None,
            high_years=(),
            low_years=(),
            reason="窗口内没有可用的 EBIT margin",
        )

    median = revenue_weighted_median([(m, r) for _, m, r in year_margins])
    if median is None:
        return CoverageResult(
            passed=False,
            covers_high=False,
            covers_low=False,
            high_threshold=None,
            low_threshold=None,
            median_margin=None,
            amplitude=None,
            high_years=(),
            low_years=(),
            reason="窗口内各年营业收入均为非正，无法确定中枢。",
        )

    amplitude = cfg.min_cycle_amplitude
    high_threshold = median + amplitude
    low_threshold = median - amplitude

    high_years = tuple(y for y, m, _ in year_margins if m >= high_threshold)
    low_years = tuple(y for y, m, _ in year_margins if m <= low_threshold)
    covers_high = len(high_years) > 0
    covers_low = len(low_years) > 0
    passed = covers_high and covers_low

    reason: str | None = None
    if not passed:
        missing = []
        if not covers_high:
            missing.append("高盈利阶段")
        if not covers_low:
            missing.append("低盈利阶段")
        margins = [m for _, m, _ in year_margins]
        spread = max(margins) - min(margins)
        reason = (
            f"窗口内未覆盖{'和'.join(missing)}："
            f"EBIT margin 中枢 {median:.2%}，全窗口极差仅 {spread:.2%}，"
            f"单侧偏离不足判定门槛 {amplitude:.2%}。"
            f"说明该窗口没有跨越完整的周期高低点。"
        )

    return CoverageResult(
        passed=passed,
        covers_high=covers_high,
        covers_low=covers_low,
        high_threshold=high_threshold.quantize(RATIO_PLACES),
        low_threshold=low_threshold.quantize(RATIO_PLACES),
        median_margin=median.quantize(RATIO_PLACES),
        amplitude=amplitude,
        high_years=high_years,
        low_years=low_years,
        reason=reason,
    )


def _year_number(year: str) -> int:
    """把年份标签转成整数。

    非四位年度（如 '2024H1'、'2024Q3'）说明调用方把半年度或季度数据传了进来。
    此时必须显式报错而非静默跳过——否则窗口会悄悄少一年，中枢照常算得出来，
    错误不会以任何形式暴露。
    """
    try:
        n = int(year)
    except ValueError as exc:
        raise ValueError(
            f"周期正常化只接受完整财务年度，收到非年度标签：{year!r}"
        ) from exc
    if not 1900 <= n <= 2999:
        raise ValueError(f"年份超出合理范围：{year!r}")
    return n


def build_window(years: Sequence[YearInput], end_year: str, span: int) -> list[YearInput]:
    """取以 end_year 结尾、跨 span 个年度的窗口。

    锚定在最新完整年度上滚动，而不是写死起止年份 —— 这样加入新的一年报后
    窗口会自动前移，不会出现「人为挑选区间」的质疑。
    """
    if span <= 0:
        raise ValueError("窗口跨度必须为正")
    end = _year_number(end_year)
    start = end - span + 1
    picked = [y for y in years if start <= _year_number(y.year) <= end]
    picked.sort(key=lambda y: _year_number(y.year))
    return picked


# ---------------------------------------------------------------- 主流程


def _compute_years(
    window: Sequence[YearInput],
    variant: Literal["reported", "adjusted"],
    cfg: NormalizationConfig,
) -> list[YearResult]:
    """算出窗口内每年的 EBIT 与 margin。此阶段还不涉及加权与中枢。"""
    results: list[YearResult] = []
    for y in window:
        r = reported_ebit(y)
        c = crosscheck_ebit(y)
        a = adjusted_ebit(y)
        used = a if variant == "adjusted" else r
        margin = ebit_margin(used, y.revenue)
        # 不可比年度保留在结果里（供敏感性分析使用），但不参与中枢
        included = y.comparable and margin is not None
        results.append(
            YearResult(
                year=y.year,
                revenue=y.revenue,
                reported_ebit=r.quantize(MONEY_PLACES),
                crosscheck_ebit=None if c is None else c.quantize(MONEY_PLACES),
                adjusted_ebit=a.quantize(MONEY_PLACES),
                crosscheck_deviation=crosscheck_deviation(y),
                ebit_used=used.quantize(MONEY_PLACES),
                ebit_variant_used=variant,
                ebit_margin=margin,
                revenue_weight=None,  # 稍后按纳入集合计回填
                phase=None,
                comparable=y.comparable,
                incomparable_reason=y.incomparable_reason,
                included_in_median=included,
            )
        )
    return results


def _apply_weights_and_phases(
    years: list[YearResult], cfg: NormalizationConfig
) -> tuple[list[YearResult], CoverageResult]:
    """按纳入集回填收入权重与高低阶段标签。"""
    included = [y for y in years if y.included_in_median and y.ebit_margin is not None]
    total_revenue = sum((y.revenue for y in included), Decimal(0))

    coverage = check_cycle_coverage(
        [(y.year, y.ebit_margin, y.revenue) for y in included if y.ebit_margin is not None],
        cfg,
    )

    high_set = set(coverage.high_years)
    low_set = set(coverage.low_years)

    out: list[YearResult] = []
    for y in years:
        if y.included_in_median and total_revenue > 0:
            weight = (y.revenue / total_revenue).quantize(RATIO_PLACES)
        else:
            weight = None
        if not y.included_in_median:
            phase = None
        elif y.year in high_set:
            phase = "high"
        elif y.year in low_set:
            phase = "low"
        else:
            phase = "normal"
        out.append(replace(y, revenue_weight=weight, phase=phase))
    return out, coverage


def _summarize(
    years: list[YearResult],
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """收入加权的周期中枢与区间。

    中位数与四分位**必须用同一种加权方式**。若中枢收入加权而 p25/p75 未加权，
    情景映射（mapping.medium.delta 取 historical_p25）拿到的区间与中枢就来自两套
    口径，两者之差不再有经济含义。
    """
    pairs = [
        (y.ebit_margin, y.revenue)
        for y in years
        if y.included_in_median and y.ebit_margin is not None
    ]

    def weighted(q: str) -> Decimal | None:
        v = revenue_weighted_quantile(pairs, Decimal(q))
        return None if v is None else v.quantize(RATIO_PLACES)

    return weighted("0.5"), weighted("0.25"), weighted("0.75")


def _fail(
    status: Status,
    reason: str,
    *,
    cfg: NormalizationConfig,
    window_mode: Literal["primary_8y", "fallback_10y"] | None,
    window: Sequence[YearInput],
    years: list[YearResult],
    coverage: CoverageResult | None,
    variant: Literal["reported", "adjusted"],
    max_deviation: Decimal | None,
) -> NormalizationResult:
    """构造失败结果。**刻意不返回任何中枢值** —— 下游 DCF 拿不到数就只能拒绝计算。"""
    return NormalizationResult(
        status=status,
        window_mode=window_mode,
        window_start=window[0].year if window else None,
        window_end=window[-1].year if window else None,
        years_available=len(window),
        comparable_years=sum(1 for y in years if y.included_in_median),
        coverage=coverage,
        ebit_margin_mid=None,
        ebit_margin_p25=None,
        ebit_margin_p75=None,
        years=tuple(years),
        ebit_variant=variant,
        ebit_formula=EBIT_FORMULAS[variant],
        crosscheck_formula=CROSSCHECK_FORMULA,
        crosscheck_max_deviation=max_deviation,
        crosscheck_needs_review=(
            max_deviation is not None and max_deviation > cfg.crosscheck_tolerance
        ),
        insufficient_reason=reason,
        formula="（未产出中枢值）",
    )


def normalize(
    years: Sequence[YearInput],
    latest_year: str,
    cfg: NormalizationConfig | None = None,
) -> NormalizationResult:
    """执行周期正常化。

    流程：主窗口（8 年）→ 可比年度与覆盖校验 → 不通过则回退窗口（10 年）
    → 仍不通过则返回 NORMALIZATION_INSUFFICIENT_DATA，**不输出任何中枢值**。
    """
    cfg = cfg or NormalizationConfig()

    if not years:
        return _fail(
            NORMALIZATION_INSUFFICIENT_DATA,
            "没有任何年度数据，无法建立周期窗口。",
            cfg=cfg,
            window_mode=None,
            window=[],
            years=[],
            coverage=None,
            variant="reported",
            max_deviation=None,
        )

    # 调整口径只有在会计逐笔批准后才允许使用。
    # 用**最宽的候选窗口**做检验：只要有一年未获批准就不切换。若只检查主窗口，
    # 主窗口不通过而回退到 10 年窗口时，新纳入的两年可能根本没被批准过。
    variant: Literal["reported", "adjusted"] = cfg.default_ebit_variant
    if variant == "adjusted":
        widest = build_window(years, latest_year, cfg.fallback_years)
        if not widest or not all(y.adjustment_approved for y in widest):
            variant = "reported"

    attempts = (
        ("primary_8y", cfg.preferred_years, cfg.min_comparable_years),
        ("fallback_10y", cfg.fallback_years, cfg.min_comparable_years_fallback),
    )

    last_reason = "未进入任何窗口校验。"
    last: NormalizationResult | None = None

    for mode, span, min_comparable in attempts:
        window = build_window(years, latest_year, span)
        computed = _compute_years(window, variant, cfg)
        with_weights, coverage = _apply_weights_and_phases(computed, cfg)

        deviations = [y.crosscheck_deviation for y in with_weights if y.crosscheck_deviation is not None]
        max_dev = max(deviations) if deviations else None

        comparable_n = sum(1 for y in with_weights if y.included_in_median)

        # 校验一：可比年度数
        if comparable_n < min_comparable:
            last_reason = (
                f"{span} 年窗口内仅 {comparable_n} 个可比年度，"
                f"低于该窗口要求的 {min_comparable} 个。"
            )
            last = _fail(
                NORMALIZATION_INSUFFICIENT_DATA,
                last_reason,
                cfg=cfg,
                window_mode=mode,  # type: ignore[arg-type]
                window=window,
                years=with_weights,
                coverage=coverage,
                variant=variant,
                max_deviation=max_dev,
            )
            continue

        # 校验二：周期高低阶段覆盖
        if not coverage.passed:
            last_reason = coverage.reason or "周期覆盖校验未通过。"
            last = _fail(
                INCOMPLETE_CYCLE,
                last_reason,
                cfg=cfg,
                window_mode=mode,  # type: ignore[arg-type]
                window=window,
                years=with_weights,
                coverage=coverage,
                variant=variant,
                max_deviation=max_dev,
            )
            continue

        mid, p25, p75 = _summarize(with_weights)
        if mid is None:
            last_reason = "窗口内没有可用的 EBIT margin，无法计算中枢。"
            last = _fail(
                NORMALIZATION_INSUFFICIENT_DATA,
                last_reason,
                cfg=cfg,
                window_mode=mode,  # type: ignore[arg-type]
                window=window,
                years=with_weights,
                coverage=coverage,
                variant=variant,
                max_deviation=max_dev,
            )
            continue

        return NormalizationResult(
            status=NORMALIZED if mode == "primary_8y" else EXTENDED_NORMALIZED,
            window_mode=mode,  # type: ignore[arg-type]
            window_start=window[0].year,
            window_end=window[-1].year,
            years_available=len(window),
            comparable_years=comparable_n,
            coverage=coverage,
            ebit_margin_mid=mid,
            ebit_margin_p25=p25,
            ebit_margin_p75=p75,
            years=tuple(with_weights),
            ebit_variant=variant,
            ebit_formula=EBIT_FORMULAS[variant],
            crosscheck_formula=CROSSCHECK_FORMULA,
            crosscheck_max_deviation=max_dev,
            crosscheck_needs_review=(
                max_dev is not None and max_dev > cfg.crosscheck_tolerance
            ),
            insufficient_reason=None,
            formula="Σ(各年营业收入权重 × 各年 EBIT margin) 的加权中位数",
        )

    assert last is not None
    return last
