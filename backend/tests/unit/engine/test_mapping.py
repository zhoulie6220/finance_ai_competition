"""指数 → 估值情景传导的测试。

最要紧的一条是**这个数据结构里没有目标价字段**。

赛事把「计算可复算、过程可追溯」写成硬要求。一个会自己吐目标价的指数
恰恰是它的反面——用户看到数字变了，却不知道是谁改的、凭什么改。
所以这条不靠自觉，靠类型层面的保证，并且用测试钉死。
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from app.engine.mapping import (
    ScenarioAdjustment,
    DEFAULT_WEIGHTS,
    NOTE_NO_WACC,
    ScenarioWeights,
    map_index_to_scenarios,
    parse_weights,
)

D = Decimal


# ---------------------------------------------------------------- 不许有目标价


def test_adjustment_has_no_price_field() -> None:
    """★ 传导结果里**不能有**任何价格字段。

    加「目标价」不是加一个字段的事，而是把「估值永远是区间」这个设计
    改掉了。真要有这么个字段，它迟早会被人填上、被页面渲染出来，
    然后整条「不做投资建议」的边界就没了。
    """
    names = {f.name for f in dataclasses.fields(ScenarioWeights)}
    names |= {f.name for f in dataclasses.fields(
        type(map_index_to_scenarios("high"))
    )}
    forbidden = {"price", "target_price", "target", "fair_value", "value_per_share"}
    assert not (names & forbidden), f"传导结果里出现了价格字段：{names & forbidden}"


def test_weights_are_weights_not_amounts() -> None:
    """权重之和恒为 1——它们描述的是概率分布，不是金额。"""
    for grade in ("high", "medium", "low"):
        adj = map_index_to_scenarios(grade)          # type: ignore[arg-type]
        assert adj.weights is not None
        w = adj.weights
        assert abs((w.base + w.optimistic + w.stress) - D(1)) < D("1e-9")


def test_weights_reject_a_sum_other_than_one() -> None:
    """和不等于 1 的权重会被拒绝。

    每个单独看都像正常数字，加起来不是 100% —— 而且不报错。
    """
    with pytest.raises(ValueError, match="之和必须为 1"):
        ScenarioWeights(D("0.6"), D("0.25"), D("0.2"))


def test_weights_reject_out_of_range() -> None:
    with pytest.raises(ValueError, match="不在"):
        ScenarioWeights(D("1.6"), D("-0.3"), D("-0.3"))


# ---------------------------------------------------------------- 四个等级


def test_high_keeps_the_base_scenario() -> None:
    """≥70：保留原有情景，**不得自动提高增长率**。"""
    adj = map_index_to_scenarios("high")
    assert adj.weights == DEFAULT_WEIGHTS["high"]
    assert "不得自动提高增长率" in adj.valuation_action
    assert adj.requires_human_confirmation is True


def test_medium_asks_for_confirmation_and_does_not_change_values() -> None:
    """45–70：请求确认相关假设，**不自动改值**。"""
    adj = map_index_to_scenarios("medium")
    assert "不自动改值" in adj.valuation_action
    assert adj.requires_human_confirmation is True


def test_low_needs_human_confirmed_parameters() -> None:
    """<45：人工确认具体经营参数后重算。"""
    adj = map_index_to_scenarios("low")
    assert "人工确认" in adj.valuation_action
    assert adj.weights == DEFAULT_WEIGHTS["low"]


def test_insufficient_evidence_does_not_touch_valuation() -> None:
    """★ 不足以出分 → **不发生由指数驱动的估值调整**。

    包括情景权重：给一组「差不多的权重」会让闸门形同虚设——
    分数都没出，却已经在影响估值了。
    """
    adj = map_index_to_scenarios("insufficient_evidence")
    assert adj.changes_valuation is False
    assert adj.weights is None
    assert adj.revenue_growth_ref is None
    assert "不发生由指数驱动的估值调整" in adj.valuation_action


# ---------------------------------------------------------------- 参数边界


def test_wacc_and_terminal_growth_are_never_touched() -> None:
    """★ 传导**不涉及** WACC 与永续增长率。

    旧稿让指数自动上调 WACC +0.5/+1.0 个百分点、下调终值增长率
    −0.5 个百分点；v1.1 §A.7 明确禁止——这两个参数需要独立依据，
    不得仅因叙事指数低而修改。

    测的是**结构**：传导结果里根本没有承载这两个参数的字段，
    而不是「文案里不出现 wacc 字样」——约束说明本来就该提到它们
    （说明它们没被改），按字样判会把正确的说明判成违规。
    """
    field_names = {f.name for f in dataclasses.fields(ScenarioAdjustment)}
    for forbidden in ("wacc", "wacc_delta", "terminal_growth", "terminal_growth_delta"):
        assert forbidden not in field_names, f"传导结果里出现了 {forbidden} 字段"

    for grade in ("high", "medium", "low", "insufficient_evidence"):
        adj = map_index_to_scenarios(grade)          # type: ignore[arg-type]
        joined = " ".join(adj.notes)
        # 不但不能改，还要**明说**没改——复核的人得看得见这条边界
        assert "不得仅因叙事指数低而修改" in joined


def test_every_result_carries_the_reproducibility_notes() -> None:
    """每次传导都带上三条约束说明，不能只给一个权重表。

    复核的人不能只看到「权重变了」，还得看到「哪些参数**没有**被改、为什么」。
    """
    for grade in ("high", "medium", "low", "insufficient_evidence"):
        adj = map_index_to_scenarios(grade)          # type: ignore[arg-type]
        assert NOTE_NO_WACC in adj.notes
        joined = " ".join(adj.notes)
        assert "主观假设" in joined, "没说明情景权重是主观假设"
        assert "重复惩罚" in joined, "没说明重复惩罚的约束"


def test_growth_reference_is_a_pointer_not_a_number() -> None:
    """传导给出的是**统计口径的引用**，不是具体数值。

    给具体数值就是在替用户做假设——而假设必须由人工确认。

    ⚠ 别用「不含数字」来判：`historical_p25` 里的 25 是**百分位名称**
    不是取值，按数字判会把正确的引用判成违规。
    """
    known_refs = {
        "historical_median",
        "historical_p25",
        "historical_p25_x0.9",
        "historical_p75",
    }
    for grade in ("high", "medium", "low"):
        adj = map_index_to_scenarios(grade)          # type: ignore[arg-type]
        assert adj.revenue_growth_ref in known_refs, (
            f"{adj.revenue_growth_ref!r} 不是已知的统计口径引用"
        )


# ---------------------------------------------------------------- 解析


def test_parse_weights_from_rule_config_format() -> None:
    w = parse_weights("[0.60,0.25,0.15]")
    assert (w.base, w.optimistic, w.stress) == (D("0.60"), D("0.25"), D("0.15"))


def test_parse_weights_rejects_wrong_count() -> None:
    """个数不对就抛错，**不给默认值**。

    静默回退到默认权重会让「参数被改坏了」表现为「参数没生效」——
    页面上看不出任何异常。
    """
    with pytest.raises(ValueError, match="需要 3 个数"):
        parse_weights("[0.6,0.4]")


def test_custom_weights_are_used_when_given() -> None:
    """调用方给了权重就用它的——skill 层应当总是从 rule_config 读。"""
    custom = {
        "high": ScenarioWeights(D("0.7"), D("0.2"), D("0.1")),
        "medium": DEFAULT_WEIGHTS["medium"],
        "low": DEFAULT_WEIGHTS["low"],
        "insufficient_evidence": DEFAULT_WEIGHTS["insufficient_evidence"],
    }
    assert map_index_to_scenarios("high", weights_by_grade=custom).weights == custom["high"]
