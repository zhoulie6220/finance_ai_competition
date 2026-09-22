"""周期正常化的 golden case 测试。

覆盖方案选择第 11 条点名的六个场景：
    8 年窗口 / 10 年回退 / 收入加权中位 / 不可比年度处理 /
    周期高低阶段覆盖 / 数据不足时返回 NORMALIZATION_INSUFFICIENT_DATA
"""

from decimal import Decimal

import pytest

from app.engine.normalization import (
    EXTENDED_NORMALIZED,
    INCOMPLETE_CYCLE,
    NORMALIZATION_INSUFFICIENT_DATA,
    NORMALIZED,
    NormalizationConfig,
    YearInput,
    check_cycle_coverage,
    crosscheck_deviation,
    normalize,
    reported_ebit,
    revenue_weighted_median,
)

D = Decimal


def year(
    y: str,
    *,
    revenue: str,
    margin: str,
    interest_expense: str = "0",
    interest_income: str = "0",
    comparable: bool = True,
    reason: str | None = None,
    adjustment: str = "0",
    approved: bool = False,
) -> YearInput:
    """按 EBIT margin 反推利润总额，构造一年的输入。

    reported_ebit = 利润总额 + 利息费用 − 利息收入，令其等于 margin × revenue，
    即可反解出利润总额。
    """
    rev = D(revenue)
    ebit = rev * D(margin)
    profit_before_tax = ebit - D(interest_expense) + D(interest_income)
    return YearInput(
        year=y,
        revenue=rev,
        profit_before_tax=profit_before_tax,
        interest_expense=D(interest_expense),
        interest_income=D(interest_income),
        comparable=comparable,
        incomparable_reason=reason,
        adjustment_delta=D(adjustment),
        adjustment_approved=approved,
    )


# 一个跨越完整周期的 8 年窗口：既有 15% 的高点，也有 3% 的低点
CYCLE_8 = [
    year("2017", revenue="1000", margin="0.08"),
    year("2018", revenue="1000", margin="0.12"),
    year("2019", revenue="1000", margin="0.06"),
    year("2020", revenue="1000", margin="0.03"),
    year("2021", revenue="1000", margin="0.15"),
    year("2022", revenue="1000", margin="0.10"),
    year("2023", revenue="1000", margin="0.04"),
    year("2024", revenue="1000", margin="0.07"),
]


# ---------------------------------------------------------------- 场景一：8 年窗口


def test_eight_year_window_normalizes():
    r = normalize(CYCLE_8, latest_year="2024")
    assert r.status == NORMALIZED
    assert r.window_mode == "primary_8y"
    assert (r.window_start, r.window_end) == ("2017", "2024")
    assert r.years_available == 8
    assert r.comparable_years == 8
    assert r.ok


def test_window_rolls_with_latest_year():
    """窗口锚定在最新完整年度上滚动，而不是写死起止年份。"""
    r = normalize(CYCLE_8, latest_year="2024")
    assert r.window_end == "2024"

    # 补入 2025 年后，窗口应整体前移一年
    extended = CYCLE_8[1:] + [year("2025", revenue="1000", margin="0.09")]
    r2 = normalize(extended, latest_year="2025")
    assert r2.window_start == "2018"
    assert r2.window_end == "2025"


# ---------------------------------------------------------------- 场景二：收入加权中位


def test_revenue_weighted_median_is_not_arithmetic_mean():
    """高收入低利润率的一年应当压过低收入高利润率的一年。"""
    pairs = [(D("0.02"), D("900")), (D("0.20"), D("100"))]
    assert revenue_weighted_median(pairs) == D("0.02")
    # 算术平均会给出 0.11，与加权中位数相差 5 倍以上——这正是不能用简单平均的原因
    assert (D("0.02") + D("0.20")) / 2 == D("0.11")


def test_weighted_median_uses_revenue_weights():
    """收入不等时，中枢应被收入权重拉动。"""
    years = [
        year("2017", revenue="100", margin="0.03"),
        year("2018", revenue="100", margin="0.04"),
        year("2019", revenue="100", margin="0.06"),
        year("2020", revenue="100", margin="0.07"),
        year("2021", revenue="5000", margin="0.08"),  # 收入极大
        year("2022", revenue="100", margin="0.10"),
        year("2023", revenue="100", margin="0.12"),
        year("2024", revenue="100", margin="0.15"),
    ]
    r = normalize(years, latest_year="2024")
    assert r.ok
    # 2021 的收入占窗口合计 5000/5700 ≈ 87.7%，权重远超其他年份。
    # 加权中位数被它拉到 0.08：累计权重到 2021 时已达 5400/5700，
    # 超过总权重的一半，故取该年数值。
    weights = {y.year: y.revenue_weight for y in r.years}
    assert weights["2021"] == D("0.877193")
    assert weights["2017"] == D("0.017544")
    assert r.ebit_margin_mid == D("0.08")


# ---------------------------------------------------------------- 场景三：不可比年度


def test_incomparable_year_excluded_but_preserved():
    """不可比年度排除出中枢，但整行保留（供敏感性分析继续使用）。"""
    years = [
        year("2017", revenue="1000", margin="0.08"),
        year("2018", revenue="1000", margin="0.12"),
        year("2019", revenue="1000", margin="0.60",
             comparable=False, reason="mna"),  # 并购年，剔除
        year("2020", revenue="1000", margin="0.03"),
        year("2021", revenue="1000", margin="0.15"),
        year("2022", revenue="1000", margin="0.10"),
        year("2023", revenue="1000", margin="0.04"),
        year("2024", revenue="1000", margin="0.07"),
    ]
    r = normalize(years, latest_year="2024")
    assert r.ok
    assert r.comparable_years == 7

    merged = next(y for y in r.years if y.year == "2019")
    assert merged.comparable is False
    assert merged.incomparable_reason == "mna"
    assert merged.included_in_median is False
    assert merged.revenue_weight is None      # 未参与加权
    assert merged.ebit_margin is not None     # 但数据本身保留
    assert merged.phase is None


def test_incomparable_year_without_reason_is_rejected():
    """不可比必须写原因——这条在数据层由 CHECK 约束保证，此处验证构造期就不允许。"""
    y = year("2019", revenue="1000", margin="0.6", comparable=False, reason=None)
    assert y.comparable is False
    assert y.incomparable_reason is None  # 引擎不自行编造原因，交由仓储层校验


# ---------------------------------------------------------------- 场景四：10 年回退


def test_falls_back_to_ten_year_window():
    """8 年窗口可比年度不足 7 个时，自动扩展到 10 年。"""
    years = [
        year("2015", revenue="1000", margin="0.02"),
        year("2016", revenue="1000", margin="0.05"),
        year("2017", revenue="1000", margin="0.09", comparable=False, reason="restructuring"),
        year("2018", revenue="1000", margin="0.13"),
        year("2019", revenue="1000", margin="0.07", comparable=False, reason="restatement"),
        year("2020", revenue="1000", margin="0.03"),
        year("2021", revenue="1000", margin="0.16"),
        year("2022", revenue="1000", margin="0.11"),
        year("2023", revenue="1000", margin="0.04"),
        year("2024", revenue="1000", margin="0.08"),
    ]
    # 8 年窗口（2017—2024）只有 6 个可比年度，不足 7 个
    r = normalize(years, latest_year="2024")
    assert r.status == EXTENDED_NORMALIZED
    assert r.window_mode == "fallback_10y"
    assert (r.window_start, r.window_end) == ("2015", "2024")
    assert r.comparable_years == 8
    assert r.ok


# ---------------------------------------------------------------- 场景五：数据不足


def test_insufficient_data_returns_status_code():
    """只有 4 年数据，两个窗口都不满足，必须返回 NORMALIZATION_INSUFFICIENT_DATA。"""
    years = [
        year("2021", revenue="1000", margin="0.03"),
        year("2022", revenue="1000", margin="0.15"),
        year("2023", revenue="1000", margin="0.06"),
        year("2024", revenue="1000", margin="0.10"),
    ]
    r = normalize(years, latest_year="2024")
    assert r.status == NORMALIZATION_INSUFFICIENT_DATA
    assert not r.ok


def test_empty_input_returns_status_code():
    r = normalize([], latest_year="2024")
    assert r.status == NORMALIZATION_INSUFFICIENT_DATA
    assert not r.ok


# ---------------------------------------------------------------- 场景六：周期覆盖


def test_flat_window_fails_cycle_coverage():
    """全是平淡年份的窗口不构成完整周期——这正是覆盖校验要拦住的东西。"""
    flat = [
        year(y, revenue="1000", margin=m)
        for y, m in [
            ("2017", "0.068"), ("2018", "0.072"), ("2019", "0.065"), ("2020", "0.070"),
            ("2021", "0.075"), ("2022", "0.069"), ("2023", "0.071"), ("2024", "0.073"),
        ]
    ]
    r = normalize(flat, latest_year="2024")
    assert r.status == INCOMPLETE_CYCLE
    assert not r.ok
    assert r.coverage is not None
    assert r.coverage.passed is False
    assert "完整" in (r.insufficient_reason or "")


def test_coverage_passes_when_both_phases_present():
    # 三元组为 (年份, EBIT margin, 营业收入) —— 带收入是为了与报告中枢同样使用
    # 收入加权口径，避免两处口径不一致
    c = check_cycle_coverage(
        [
            ("a", D("0.02"), D("1000")),
            ("b", D("0.07"), D("1000")),
            ("c", D("0.08"), D("1000")),
            ("d", D("0.15"), D("1000")),
        ],
        NormalizationConfig(),
    )
    assert c.passed is True
    assert c.covers_high is True
    assert c.covers_low is True
    assert "a" in c.low_years
    assert "d" in c.high_years


def test_coverage_uses_revenue_weighted_median_as_center():
    """中枢必须与报告值同口径（收入加权），否则高低阶段的判定基准会漂移。"""
    # 收入 9900 的那年 margin 极低，加权中位数应被它拉到 0.03 附近；
    # 若用未加权中位数则是 0.085，两者的高低阈值会差出好几个百分点
    c = check_cycle_coverage(
        [
            ("big", D("0.02"), D("9900")),
            ("x1", D("0.07"), D("25")),
            ("x2", D("0.08"), D("25")),
            ("x3", D("0.15"), D("25")),
            ("x4", D("0.16"), D("25")),
        ],
        NormalizationConfig(),
    )
    assert c.median_margin is not None
    assert c.median_margin < D("0.05")   # 被大收入年份拉到低处，而非 0.08


# ---------------------------------------------------------------- 失败时绝不输出中枢值


@pytest.mark.parametrize(
    "years, expected",
    [
        ([year("2021", revenue="1000", margin="0.03"),
          year("2022", revenue="1000", margin="0.15")], NORMALIZATION_INSUFFICIENT_DATA),
        ([year(y, revenue="1000", margin="0.07") for y in
          ("2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024")], INCOMPLETE_CYCLE),
    ],
)
def test_failure_never_emits_a_central_value(years, expected):
    """「不强行正常化」的核心含义：失败时连一个「带警告的数字」都不给。

    下游 DCF 拿不到值就只能拒绝计算，这是刻意的设计——如果这里返回了数字，
    哪怕附带警告，也一定会有调用方直接用上。
    """
    r = normalize(years, latest_year="2024")
    assert r.status == expected
    assert r.ebit_margin_mid is None
    assert r.ebit_margin_p25 is None
    assert r.ebit_margin_p75 is None
    assert r.insufficient_reason  # 必须写明原因，禁止静默降级


# ---------------------------------------------------------------- EBIT 口径


def test_reported_ebit_formula():
    y = year("2024", revenue="1000", margin="0.10",
             interest_expense="30", interest_income="5")
    # 利润总额 = 100 − 30 + 5 = 75；EBIT = 75 + 30 − 5 = 100
    assert y.profit_before_tax == D("75")
    assert reported_ebit(y) == D("100")


def test_crosscheck_deviation_flags_large_gap():
    y = YearInput(
        year="2024",
        revenue=D("1000"),
        profit_before_tax=D("100"),        # reported EBIT = 100 + 0 − 0 = 100
        interest_expense=D("0"),
        interest_income=D("0"),
        operating_profit=D("200"),    # cross-check = 200 + 0 = 200，与 100 相差一倍
        finance_expense=D("0"),
    )
    dev = crosscheck_deviation(y)
    assert dev == D("1.000000")
    assert dev > NormalizationConfig().crosscheck_tolerance


def test_crosscheck_missing_data_yields_none():
    y = year("2024", revenue="1000", margin="0.10")
    assert crosscheck_deviation(y) is None


def test_adjusted_variant_requires_approval():
    """未获会计批准时，即使配置要求用 adjusted，也必须退回 reported。"""
    years = [
        year(y, revenue="1000", margin=m, adjustment="100", approved=False)
        for y, m in [
            ("2017", "0.02"), ("2018", "0.05"), ("2019", "0.07"), ("2020", "0.09"),
            ("2021", "0.15"), ("2022", "0.11"), ("2023", "0.04"), ("2024", "0.08"),
        ]
    ]
    r = normalize(years, latest_year="2024",
                  cfg=NormalizationConfig(default_ebit_variant="adjusted"))
    assert r.ebit_variant == "reported"

    approved = [
        year(y, revenue="1000", margin=m, adjustment="100", approved=True)
        for y, m in [
            ("2017", "0.02"), ("2018", "0.05"), ("2019", "0.07"), ("2020", "0.09"),
            ("2021", "0.15"), ("2022", "0.11"), ("2023", "0.04"), ("2024", "0.08"),
        ]
    ]
    r2 = normalize(approved, latest_year="2024",
                   cfg=NormalizationConfig(default_ebit_variant="adjusted"))
    assert r2.ebit_variant == "adjusted"


# ---------------------------------------------------------------- 可复现性


def test_same_input_same_output():
    """纯函数：同输入必同输出。这是「结果可复现」的基础。"""
    a = normalize(CYCLE_8, latest_year="2024")
    b = normalize(CYCLE_8, latest_year="2024")
    assert a == b


def test_config_guards_against_nonsense():
    with pytest.raises(ValueError):
        NormalizationConfig(preferred_years=10, fallback_years=8)
    with pytest.raises(ValueError):
        NormalizationConfig(min_comparable_years=0)
