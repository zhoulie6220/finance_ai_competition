"""派生指标的引擎测试。

这一层是**纯函数**（零 IO、零 LLM），所以可以喂几个数进去直接断言结果，
不需要建库。本文件盯的是三类会**静默出错**的东西：

1. **量纲**——金额在库里是「百万元」、销量是「吨」，相除得到的是
   「百万元/吨」。写法上看不出问题，结果是真值的百万分之一。
2. **缺输入时的行为**——必须是 `refused` 且 `value is None`，
   不能插补成 0，也不能拿代理指标顶上。
3. **口径要两期的那些**（`roe` 要平均净资产）——缺上期时必须拒绝，
   不能拿期末值顶替。拿期末值算出来的 ROE 偏高，且看不出是哪种口径。
"""

from __future__ import annotations

from decimal import Decimal

from app.engine.derived import SPECS_BY_KEY, compute_for


def look(values: dict[tuple[str, str], str]):
    """把 `{(字段, 期间): '值'}` 包成引擎要的取值函数。"""
    table = {k: Decimal(v) for k, v in values.items()}
    return lambda metric, period: table.get((metric, period))


def test_gross_profit_is_revenue_minus_cost():
    r = compute_for("gross_profit", "2024", look({
        ("revenue", "2024"): "322116.00",
        ("operating_cost", "2024"): "304546.36",
    }))
    assert r.ok
    assert r.value == Decimal("17569.640000")
    # 出处必须是**两行**，且都指得出来——只给一行的话，
    # 用户点开看到的是半个算式
    assert [(s.metric_key, s.period) for s in r.sources] == [
        ("revenue", "2024"), ("operating_cost", "2024"),
    ]


def test_ebit_follows_the_reported_convention():
    """`利润总额 + 利息费用 − 利息收入`（会计口径 §8.1 的 Reported EBIT）。"""
    r = compute_for("ebit", "2024", look({
        ("profit_before_tax", "2024"): "9100",
        ("interest_expense", "2024"): "1625.8",
        ("interest_income", "2024"): "900",
    }))
    assert r.ok
    assert r.value == Decimal("9825.800000")


def test_missing_input_refuses_instead_of_imputing_zero():
    """★ 缺一个输入就必须拒绝，**绝不插补**。

    插补成 0 的后果不是报错，是「毛利率」变成一个偏高的数、
    而它和正常值在页面上长得一模一样。
    """
    r = compute_for("gross_profit", "2024", look({("revenue", "2024"): "322116"}))
    assert not r.ok
    assert r.value is None            # ★ 拒绝时 value 必然是 None
    assert "营业成本" in r.refused     # 理由要说清**缺的是哪一个**


def test_per_ton_converts_millions_to_yuan():
    """★ 吨钢口径的**量纲**。

    毛利入库单位是「百万元」，钢材销量是「吨」。直接相除得到的是
    「百万元/吨」——**写法上完全正常**，结果是真值的百万分之一。
    实测宝钢 2024 会算出 `0.000799 元/吨`，正确值是 **799 元/吨**。
    """
    r = compute_for("steel_gross_profit_per_ton", "2024", look({
        ("revenue", "2024"): "322116",
        ("operating_cost", "2024"): "280900",
        ("steel_sales_volume", "2024"): "51590000",
    }))
    assert r.ok
    # (322116 − 280900) 百万元 = 41,216,000,000 元 ÷ 51,590,000 吨 ≈ 798.91 元/吨
    assert Decimal("700") < r.value < Decimal("900"), (
        f"量纲不对：{r.value}。若是 0.0008 左右，说明少乘了 100 万"
    )
    assert r.unit == "元/吨"


def test_roe_needs_two_periods_and_refuses_without_the_prior():
    """★ ROE 是**平均净资产口径**（字典的 `note` 写明的）。

    缺上期时拒绝，**不拿期末权益顶替**：拿期末算出来的 ROE 偏高，
    而页面上看不出用的是哪一种口径。
    """
    both = compute_for("roe", "2024", look({
        ("net_income_parent", "2024"): "7362",
        ("equity_parent", "2024"): "200548",
        ("equity_parent", "2023"): "200325",
    }))
    assert both.ok
    # 7362 ÷ ((200548 + 200325) / 2) × 100 = 3.6729…
    assert both.value.quantize(Decimal("0.001")) == Decimal("3.673")

    only_now = compute_for("roe", "2024", look({
        ("net_income_parent", "2024"): "7362",
        ("equity_parent", "2024"): "200548",
    }))
    assert not only_now.ok
    assert only_now.value is None
    assert "2023" in only_now.refused


def test_ebitda_needs_all_three_depreciation_rows():
    """★ EBITDA 的折旧摊销是**三行相加**，缺一不可。

    只加其中两行会得到一个偏小的 EBITDA，而它和完整版长得一模一样
    （会计 2026-10-06 答复：使用权资产与无形资产是两件事，不并入也不冒充）。
    """
    base = {
        ("profit_before_tax", "2024"): "9100",
        ("interest_expense", "2024"): "1625.8",
        ("interest_income", "2024"): "900",
        ("depreciation_amortization", "2024"): "18555.8",
        ("amortization_intangible", "2024"): "485.8",
    }
    partial = compute_for("ebitda", "2024", look(base))
    assert not partial.ok
    assert "使用权资产折旧" in partial.refused

    full = compute_for("ebitda", "2024", look(
        {**base, ("right_of_use_asset_depreciation", "2024"): "418.8"}
    ))
    assert full.ok
    assert full.value == Decimal("9825.800000") + Decimal("19460.400000")


def test_every_spec_refusal_carries_a_reason():
    """★ **拒绝必须有理由。**

    没有理由的话，「缺一个字段」和「这个指标压根不适用」在页面上
    都是「—」，而这两件事该做的事完全不同：一个去补数据，一个不用管。
    """
    for key in SPECS_BY_KEY:
        r = compute_for(key, "2024", look({}))
        assert r is not None
        assert not r.ok, f"{key} 在完全没有输入时不该算出值"
        assert r.refused and len(r.refused) > 8, f"{key} 的拒绝理由太短或为空"


def test_not_a_derived_metric_returns_none():
    """不是派生字段返回 None——调用方据此跳过，不要误当成「拒绝了」。"""
    assert compute_for("revenue", "2024", look({})) is None
