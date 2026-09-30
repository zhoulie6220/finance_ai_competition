"""主张 × 指标 × 期间 的判定。

纯函数，零 IO。

**判定顺序就是全部规格。** 同一个输入按不同顺序判会得出完全不同的结论，
而且大多都不会报错。会计口径 v1.1 §A.2 / §A.5 定下来的顺序是：

    1. 主判据未披露            → unverifiable（不是「没变化」）
    2. 不可比 / 口径不一致      → needs_review（不是「相悖」）
    3. 目标期间未到             → pending（不进分母、不判冲突）
    4. **明确数值目标**         → 达标 supported / 未达标 contradicted
                                 ⚠ 噪声带**不适用于目标**
    5. 「约 / 左右」无公开容差  → 只展示偏差 + needs_review
    6. 方向性主张               → 走噪声带分类，同向 supported / 反向 contradicted
    7. 佐证指标只能「软化」冲突，**不能制造冲突**

第 4 条与第 6 条的分工最容易搞反：「收入增长至少 5%」实际增长 4%，
只差 1 个百分点，落在金额噪声带（1%）之外、但人眼看上去很接近——
**必须判未达成**。反过来，「销量增长」没有数值目标，实际变化 0.8%
就要按噪声带判 neutral，**不能判成反向冲突**。

噪声带按指标类型分派（v1.1 §A.5），共用一个会让某一类宽出两个数量级：

    金额 / 数量   相对变化 ≤ 1%
    比例（0–1）   绝对变化 ≤ 0.5 个百分点
    周转天数      绝对变化 ≤ 3 天
    产能利用率    绝对变化 ≤ 1 个百分点

**边界算噪声，严格大于才触发。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Verdict = Literal[
    "supported", "neutral", "contradicted", "needs_review", "unverifiable",
    "incomparable",
]
MetricKind = Literal["amount", "ratio", "days", "utilization"]
Change = Literal["up", "down", "flat"]

# 指标 → 它属于哪一类噪声带。
# 没列进来的按金额处理——那是绝大多数。
METRIC_KINDS: dict[str, MetricKind] = {
    "gross_margin": "ratio",
    "net_margin": "ratio",
    "ebit_margin": "ratio",
    "ebitda_margin": "ratio",
    "nonrecurring_share": "ratio",
    "cash_conversion": "ratio",
    "roe": "ratio",
    "roic": "ratio",
    "capacity_utilization": "utilization",
}

# 方向词 → 期望的变化方向。'improve'/'deteriorate' 描述的是「状态」，
# 对不同的指标含义不同：毛利率改善是上升，成本改善是下降。
_IMPROVING_IS_UP = ("gross_margin", "net_margin", "ebit_margin", "ebitda_margin",
                    "roe", "roic", "cash_conversion")


@dataclass(frozen=True)
class ActualValue:
    """某个指标在某个期间的实际值。"""

    metric_key: str
    period: str
    value: Decimal
    fact_id: str
    comparable: bool = True
    incomparable_reason: str | None = None


@dataclass(frozen=True)
class MatchConfig:
    """噪声带阈值。初值与 rule_config 的 narrative.* 一致。"""

    min_rel_change: Decimal = Decimal("0.01")
    min_ratio_change: Decimal = Decimal("0.005")
    min_days_change: Decimal = Decimal("3")
    min_utilization_change: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        for name in (
            "min_rel_change", "min_ratio_change", "min_days_change",
            "min_utilization_change",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 不能为负")


@dataclass(frozen=True)
class ClaimInput:
    """一条待判定的主张。只带判定需要的东西。"""

    claim_id: str
    claim_text: str
    claim_type: str
    direction: str
    period_norm: str | None
    magnitude_value: Decimal | None
    magnitude_unit: str | None
    magnitude_raw: str | None = None
    bound: Literal["exact", "at_least", "at_most", "about"] = "exact"
    primary_metric: str | None = None


@dataclass(frozen=True)
class MatchOutcome:
    """一条主张的判定结论。对应 claim_match 表的一行。"""

    claim_id: str
    metric_key: str
    claim_period: str
    fact_period: str
    verdict: Verdict
    reason: str
    confidence: float
    direction_claim: str | None = None
    direction_actual: str | None = None
    direction_consistent: bool | None = None
    magnitude_target: str | None = None
    magnitude_actual: str | None = None
    relative_deviation: Decimal | None = None
    formula: str | None = None
    inputs: tuple[str, ...] = ()

    @property
    def scores(self) -> bool:
        """是否计入 H / C 的等权平均。

        三个计分态是 supported / neutral / contradicted。
        其余三个既不计分也不进分子——但它们进不进**分母**是不同的：
        needs_review 留在分母，unverifiable 与 incomparable 不进。
        """
        return self.verdict in ("supported", "neutral", "contradicted")

    @property
    def in_denominator(self) -> bool:
        """是否进入覆盖率分母 N。

        v1.1 §A.4：N 是「已到验证期、对象和目标可识别」的主张数。
        未披露直接指标（unverifiable）与不可比（incomparable）不满足
        「目标可识别 / 属本期研究范围」，**不进分母**；
        待核查（needs_review）留在分母——它只是**还没查完**，
        把它踢出分母会让覆盖率虚高。
        """
        return self.verdict != "unverifiable" and self.verdict != "incomparable"


class RatioOutOfRange(ValueError):
    """比例指标的值不在 0–1 之间。

    ⚠ **这个必须炸掉，不能返回一个判定。** 比例若被存成百分数（10.00 而不是
    0.10），0.5 个百分点的噪声带会宽出 200 倍，于是每一个比例类判定
    都落到「无明显变化」——**全部判定被静默中和，一个冲突都报不出来**。
    那比崩掉糟得多：崩掉至少知道出事了。
    """


def metric_kind(metric_key: str) -> MetricKind:
    return METRIC_KINDS.get(metric_key, "amount")


# ---------------------------------------------------------------- 变化分类


def classify_change(
    kind: MetricKind,
    current: Decimal,
    base: Decimal,
    cfg: MatchConfig,
) -> Change | None:
    """按指标类型判断变化方向。落进噪声带就是 flat。

    返回 None 表示**算不出方向**——金额类的基期 ≤ 0 时不能算增长率
    （负基期的「增长」没有意义，算出来的百分比会误导），
    这时应当转人工复核而不是硬判。
    """
    if kind == "ratio":
        _require_ratio(current, "本期")
        _require_ratio(base, "基期")
        if abs(current - base) <= cfg.min_ratio_change:
            return "flat"
    elif kind == "days":
        if abs(current - base) <= cfg.min_days_change:
            return "flat"
    elif kind == "utilization":
        _require_ratio(current, "本期")
        _require_ratio(base, "基期")
        if abs(current - base) <= cfg.min_utilization_change:
            return "flat"
    else:  # amount
        if base <= 0:
            # 负基期的相对变化没有经济意义；按 v1.1 转人工复核，
            # 不硬算一个看起来像模像样的百分比
            return None
        if abs((current - base) / base) <= cfg.min_rel_change:
            return "flat"

    return "up" if current > base else "down"


def _require_ratio(value: Decimal, label: str) -> None:
    if not (Decimal(0) <= value <= Decimal(1)):
        raise RatioOutOfRange(
            f"{label}比例值 {value} 不在 0–1 之间。比例必须以 0–1 存储"
            f"（10.00 表示 10.00 而不是 10%）。存成百分数会让噪声带宽出 "
            f"100 倍以上，**全部比例类判定被静默中和**——所以这里直接拒绝，"
            f"不返回任何判定。"
        )


# ---------------------------------------------------------------- 判定


def is_explicit_target(claim: ClaimInput) -> bool:
    """句子里的那个数字是不是一个**目标**。

    ⚠ 这个区分非常要紧。年报里绝大多数数字是**已发生的事实**：

        2022 年，公司销售商品坯材 4,976.3 万吨。
        本期财务费用 24.7 亿元，同比增加流量 19.0 亿元。

    那是**报告**，不是承诺。把它们当成目标去核验，会拿「4,976.3 万吨」
    和「营业成本的变化率」相比——量纲完全不同，算出来的偏差毫无意义，
    然后判成「未达成」。实测中这一条让宝钢多出 60 多条假的「相悖」，
    而假的冲突比漏报更糟：它会让整张对照表失去可信度。

    真正能当目标核验的只有两类：

      · **比率型**（「增长 5% 以上」「下降不超过 10%」）——有方向、有幅度
      · **带界限的绝对量**（「不低于 100 万吨」「至少 5 亿元」）

    没有界限的绝对量只是陈述，按方向性主张处理（走噪声带）。
    """
    if claim.magnitude_value is None:
        return False
    if claim.magnitude_unit in ("%", "％", "个百分点"):
        return True
    return claim.bound in ("at_least", "at_most")


def judge(
    claim: ClaimInput,
    *,
    current: ActualValue | None,
    base: ActualValue | None,
    cfg: MatchConfig | None = None,
) -> MatchOutcome:
    """判定一条主张。

    `current` 是主张目标期间的事实，`base` 是上一期的事实——
    方向与幅度都要靠两者比较才判得出来。
    """
    cfg = cfg or MatchConfig()
    metric = claim.primary_metric or ""

    # ---- 1. 主判据未披露 -------------------------------------------------
    # ⚠ 这不是「没有变化」。把「查不到」当成「没变化」正是 v1.1 反复
    # 禁止的那件事——它会凭空给一条主张记 0 分，而 0 分在中性证据那里
    # 恰好是「无异议」。宁可标不可验证。
    if claim.primary_metric is None:
        return _out(
            claim, metric, "unverifiable",
            "该主题的主判据在主判据表里没有对应指标，按 v1.1 不降级用代理指标硬判。",
            confidence=0.0,
        )
    if current is None:
        return _out(
            claim, metric, "unverifiable",
            f"{claim.period_norm or '目标期间'} 未披露 {metric}，"
            f"**主判据未披露时不降级用代理指标判冲突**。",
            confidence=0.0,
        )

    # ---- 2. 不可比 -------------------------------------------------------
    for value, label in ((current, "本期"), (base, "基期")):
        if value is not None and not value.comparable:
            return _out(
                claim, metric, "incomparable",
                f"{label} {value.period} 的 {metric} 标记为不可比"
                f"（{value.incomparable_reason or '未注明原因'}），"
                f"按规则不参与判定、也不扣分。",
                confidence=0.0, fact_period=value.period,
            )

    kind = metric_kind(metric)

    # ---- 4. 明确数值目标：噪声带不适用 ----------------------------------
    # ⚠ 只对**真正的目标**走这条；句子里的普通数字是已发生的事实，
    # 不是承诺（见 is_explicit_target 的说明）。
    if is_explicit_target(claim):
        return _judge_explicit_target(claim, current, base, kind, cfg, metric)

    # ---- 3. 方向性主张但基期缺失 ----------------------------------------
    if base is None:
        return _out(
            claim, metric, "needs_review",
            f"缺少 {claim.period_norm} 的上期数据，无法计算变化方向，转人工复核。",
            confidence=0.0,
        )

    # ---- 5 & 6. 方向性主张 ----------------------------------------------
    assert base is not None
    actual_change = classify_change(kind, current.value, base.value, cfg)
    if actual_change is None:
        return _out(
            claim, metric, "needs_review",
            f"基期 {base.period} 的 {metric} 为 {base.value}，"
            f"非正基期不能计算相对变化，转人工复核。",
            confidence=0.0, fact_period=current.period,
        )

    expected = _expected_change(claim.direction, metric)
    if expected == "unknown":
        return _out(
            claim, metric, "needs_review",
            f"主张没有可识别的方向（原文方向判定为 {claim.direction}），转人工复核。",
            confidence=0.0, fact_period=current.period,
        )

    if actual_change == "flat":
        verdict: Verdict = "neutral"
        reason = (
            f"{metric} 从 {base.value} 变到 {current.value}，"
            f"落在噪声区间内，判为无明显变化——**不是相悖**。"
        )
    elif actual_change == expected:
        verdict = "supported"
        reason = f"{metric} 的实际变化方向与主张一致（{actual_change}）。"
    else:
        verdict = "contradicted"
        reason = (
            f"主张方向为 {claim.direction}，而 {metric} 的实际变化为 "
            f"{actual_change}（{base.value} → {current.value}），方向相反。"
        )

    return _out(
        claim, metric, verdict, reason, confidence=0.7,
        direction_actual=actual_change,
        direction_consistent=(actual_change == expected),
        fact_period=current.period,
        formula=f"{metric}：{base.period}={base.value} → {current.period}={current.value}",
        inputs=(base.fact_id, current.fact_id),
    )


def _judge_explicit_target(
    claim: ClaimInput,
    current: ActualValue,
    base: ActualValue | None,
    kind: MetricKind,
    cfg: MatchConfig,
    metric: str,
) -> MatchOutcome:
    """明确数值目标的核验。

    ★ **噪声带不适用于目标。** 「收入增长至少 5%」实际增长 4%，
    差距只有 1 个百分点、落在金额噪声带里，但**必须判未达成**——
    目标就是目标，4% 没到 5%。

    但「约 / 左右」例外：没有公开容差时只展示偏差并转人工复核，
    **不擅自认定完成或未完成**。
    """
    target = claim.magnitude_value
    assert target is not None

    if kind not in ("ratio", "amount") or current.value is None:
        return _out(
            claim, metric, "needs_review",
            f"数值目标 {claim.magnitude_raw} 的口径无法与 {metric} 直接比较，转人工复核。",
            confidence=0.0, fact_period=current.period,
        )

    # 「约 10%」「10% 左右」——没有公开容差，不擅自判完成与否
    if claim.bound == "about":
        return _out(
            claim, metric, "needs_review",
            f"原文表述为「{claim.magnitude_raw}」，属约数且无公开容差，"
            f"只展示实际偏差、不擅自认定完成或未完成。",
            confidence=0.0, magnitude_target=claim.magnitude_raw,
            fact_period=current.period,
        )

    # 目标是比例时，把它从「百分数」换成 0–1 再与实测比
    target_ratio = target / Decimal(100) if claim.magnitude_unit in ("%", "％") else target
    actual_ratio = current.value
    if base is not None and kind == "amount" and base.value > 0:
        actual_ratio = (current.value - base.value) / base.value
    elif kind == "amount":
        return _out(
            claim, metric, "needs_review",
            f"金额类目标的基期（{base.period if base else '缺'}）非正，无法计算达成率。",
            confidence=0.0, fact_period=current.period,
        )

    deviation = actual_ratio - target_ratio
    met = {
        "exact": abs(deviation) <= cfg.min_ratio_change,
        "at_least": deviation >= 0,
        "at_most": deviation <= 0,
    }.get(claim.bound, False)

    verdict: Verdict = "supported" if met else "contradicted"
    reason = (
        f"明确数值目标「{claim.magnitude_raw}」：实际 {actual_ratio:.4f}，"
        f"{'达成' if met else '**未达成**'}（偏差 {deviation:+.4f}）。"
        f"⚠ 数值目标不套用噪声带——差得少不等于达标。"
    )
    return _out(
        claim, metric, verdict, reason, confidence=0.8,
        magnitude_target=claim.magnitude_raw,
        magnitude_actual=str(current.value),
        relative_deviation=deviation.quantize(Decimal("0.000001")),
        fact_period=current.period,
        formula=(
            f"目标 {target_ratio} ({claim.bound})，实际 {actual_ratio}"
            + (f"（{base.period}={base.value} → {current.period}={current.value}）" if base else "")
        ),
        inputs=tuple(f.fact_id for f in (base, current) if f),
    )


def _expected_change(direction: str, metric: str) -> Change | Literal["unknown"]:
    """主张方向 → 期望的数值变化方向。

    ⚠ 「改善」对不同的指标含义相反：毛利率改善是**上升**，
    成本改善是**下降**。按词面统一处理会让一半的判定方向反过来，
    而且不报错。
    """
    if direction == "up":
        return "up"
    if direction == "down":
        return "down"
    if direction == "flat":
        return "flat"
    if direction == "improve":
        return "up" if metric in _IMPROVING_IS_UP else "down"
    if direction == "deteriorate":
        return "down" if metric in _IMPROVING_IS_UP else "up"
    return "unknown"


def _out(
    claim: ClaimInput,
    metric: str,
    verdict: Verdict,
    reason: str,
    *,
    confidence: float,
    fact_period: str | None = None,
    direction_actual: str | None = None,
    direction_consistent: bool | None = None,
    magnitude_target: str | None = None,
    magnitude_actual: str | None = None,
    relative_deviation: Decimal | None = None,
    formula: str | None = None,
    inputs: tuple[str, ...] = (),
) -> MatchOutcome:
    return MatchOutcome(
        claim_id=claim.claim_id,
        metric_key=metric,
        claim_period=claim.period_norm or "",
        fact_period=fact_period or claim.period_norm or "",
        verdict=verdict,
        reason=reason,
        confidence=confidence,
        direction_claim=claim.direction,
        direction_actual=direction_actual,
        direction_consistent=direction_consistent,
        magnitude_target=magnitude_target,
        magnitude_actual=magnitude_actual,
        relative_deviation=relative_deviation,
        formula=formula,
        inputs=inputs,
    )


# ---------------------------------------------------------------- 覆盖统计


def summarize(outcomes: tuple[MatchOutcome, ...]) -> dict[str, int]:
    """按判定分组计数。分母口径见 MatchOutcome.in_denominator。"""
    counts: dict[str, int] = {v: 0 for v in
                              ("supported", "neutral", "contradicted",
                               "needs_review", "unverifiable", "incomparable")}
    for o in outcomes:
        counts[o.verdict] = counts.get(o.verdict, 0) + 1
    # N 与 n 的定义写在返回值里，避免调用方各自解释
    counts["n"] = counts["supported"] + counts["neutral"] + counts["contradicted"]
    counts["N"] = sum(1 for o in outcomes if o.in_denominator)
    counts["pending_and_unresolved"] = counts["needs_review"]
    return counts


__all__ = [
    "ActualValue",
    "ClaimInput",
    "MatchConfig",
    "MatchOutcome",
    "MetricKind",
    "RatioOutOfRange",
    "Verdict",
    "classify_change",
    "judge",
    "metric_kind",
    "summarize",
]
