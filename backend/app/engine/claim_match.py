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

#: 比例型的单位。只有目标是比例时，才能直接和实测的变化率相比。
_RATIO_UNITS = ("%", "％", "个百分点")

#: 目标数的单位 → 量纲。用来挡住「目标写亿元、主判据却是钢材销量（吨）」
#: 这类比较——那两个数放在一起减，算得出偏差、写得出理由，
#: 和真结论长得一模一样。（实测宝钢真实撞到过：一句「营业总收入较计划
#: 减少 298.46 亿元」的主判据被映射成了钢材销量。）
_UNIT_KIND: dict[str, str] = {
    "元": "currency", "万元": "currency", "百万元": "currency",
    "亿元": "currency", "万亿元": "currency",
    "吨": "ton", "万吨": "ton", "亿吨": "ton",
}
_UNIT_KIND_CN: dict[str, str] = {"currency": "金额", "ton": "吨", "percent": "比例"}

#: 折算成「百万元」的因子。财务事实库里金额一律存百万元。
#:
#: ⚠ **只有金额类有因子。** 会计口径 8-1 只授权了「1 亿元 = 100 百万元」，
#: 吨 / 万吨 的换算法（事实库里存的是吨还是万吨）**没有定**，
#: 所以这里不给——拿不到因子就走人工复核，**不猜一个看起来合理的数**。
_MILLION_FACTOR: dict[str, Decimal] = {
    "元": Decimal("0.000001"),
    "万元": Decimal("0.01"),
    "百万元": Decimal("1"),
    "亿元": Decimal("100"),
    "万亿元": Decimal("1000000"),
}

#: 目标的达成方向，取自 `metric_definition.sign_convention`（v1.1 §二）。
#: v1.1 问答 8-2 给了它的用法：
#:
#:     成本类（越小越好）  实际 ≤ 计划 → 达成；实际 > 计划 → 未达成
#:     收入类（越大越好）  实际 ≥ 计划 → 达成；实际 < 计划 → 未达成
#:
#: 没列进来的（`neutral`）方向不明 → 不判方向，只报偏差。
_SIGN_TO_BETTER: dict[str, Literal["higher", "lower"]] = {
    "positive_is_good": "higher",
    "negative_is_good": "lower",
}

# 方向词 → 期望的变化方向。'improve'/'deteriorate' 描述的是「状态」，
# 对不同的指标含义不同：毛利率改善是上升，成本改善是下降。
#
# ⚠ **不在这个表里的一律按「改善 = 下降」**，所以漏一个就是方向整个反掉。
# 「钢材销量」原来就不在里面：`extract_direction("销量改善")` 得到 improve，
# 这里映射成 **down**，而销量实际是上升的——判出来是「相悖」，
# 理由是「steel_sales_volume 的实际变化为 up，方向相反」，**看着完全正常**。
# 同一棵树上「回款改善」＝应收下降、「成本改善」＝成本下降，默认值对它们是对的，
# 对的越多越不容易发现漏了谁。
#
# 判据：这个指标的「改善」是不是意味着**数值变大**。
# 量（销量、收入、产量）是；代价（成本、费用、应收、天数）不是。
_IMPROVING_IS_UP = (
    # 利润率类：改善就是变大
    "gross_margin", "net_margin", "ebit_margin", "ebitda_margin",
    "roe", "roic", "cash_conversion",
    # 规模类：卖得更多、收得更多，就是改善
    "steel_sales_volume", "steel_output",
    "revenue", "total_revenue", "cfo",
    "capacity_utilization",
)


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
    #: 这个数字是不是**计划值**。判定要区分「承诺」和「已发生的事实」——
    #: 拿事实去核验事实永远判「支持」，而假的「支持」看不出来。
    is_plan: bool = False
    #: 主判据的**量纲**与 **sign_convention**，来自 `metric_definition`。
    #: 绝对量目标要和事实比，就得知道主判据是什么量纲、往哪个方向算「好」——
    #: 这两样都是会计口径里的字段（§二 / §A-1），不在这一层另立一套。
    metric_unit_kind: str | None = None
    metric_sign: str | None = None
    #: 这个目标数字是不是**从主判据别名旁边**取来的（`claim_rules.Magnitude`）。
    #: False 表示这句话里的数与该指标在文字上没有关联——例如
    #: 「预算安排固定资产投资资金239.2亿元」被映射成了营业成本。
    #: 那种情况下目标与事实不是同一件事，**不能比**（会计口径 8-3 第 1 条
    #: 要求的「先核对业务范围和期间」正是这一步）。
    target_metric_aligned: bool = True
    #: 这一条的判据**不是**主题原本的主判据，而是会计授权的回退指标。
    #:
    #: 目前只有一处：华菱 / 首钢年报的行业聚合销量（`industry_sales_volume`）。
    #: 会计 2026-10-06 答复「改走 B，但加严格限制」——允许用它做
    #: 「需求与产销」的方向判断与 Q3，**前提是保留出处并标明口径存疑**。
    #: 所以这一位不只是个布尔：它决定判定理由里必须多出一段披露，
    #: **没有那段披露，这条判定看上去和用钢材销量判出来的一模一样。**
    metric_substituted: bool = False


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
    #: —— 绝对量目标专用。会计口径 8-1 要求「原始数值、原始单位、
    #: 换算因子、标准化数值」四样都留痕，前两样在 claim 表上
    #: （`magnitude_value` / `magnitude_unit`），这里留后两样。
    target_unit: str | None = None
    target_millions: Decimal | None = None
    unit_factor: Decimal | None = None
    #: 「原始计划偏差」= 实际 − 换算后的目标（百万元）。
    #: 8-3 第 4 条：**可以展示，但不进 H 的支持/相悖判定**——
    #: 所以它旁边的 verdict 一定是 needs_review，不会是 supported/contradicted。
    plan_variance: Decimal | None = None
    #: 按 8-2 的方向算出来的**参考结论**（「实际高于计划，属未达成」这类）。
    #: 只给人看，**不是判定**——它不进 H，见 `_judge_absolute_target`。
    plan_reference: str | None = None

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

    真正能当目标核验的有三类：

      · **比率型**（「增长 5% 以上」「下降不超过 10%」）——有方向、有幅度
      · **带界限的绝对量**（「不低于 100 万吨」「至少 5 亿元」）
      · **计划里的绝对量**（「2018年公司计划营业成本 2,420 亿元」）
        —— 有「计划 / 预算 / 目标」这类模态词，说明它是承诺不是陈述

    没有界限的绝对量只是陈述，按方向性主张处理（走噪声带）。

    ⚠ 第三类**不是**在放宽上面那条警告。警告说的是「拿 4,976.3 万吨
    去和营业成本的变化率相比」——量纲不同。而这里能进第三类的前提是
    抽取阶段已经**按主判据别名挑过数**（`claim_rules._pick_by_metric`）：
    「营业成本 2,420 亿元」对「营业成本 2,590.85 亿元」，同一个指标、
    同一个量纲，比较是有意义的。

    实测：宝钢 2018 年计划营业成本 2,420 亿元，实际 2,590.85 亿元，
    超支 7%——这是一个**真实存在**、原先被判成「转人工复核」的未达成。
    """
    if claim.magnitude_value is None:
        return False
    if claim.magnitude_unit in ("%", "％", "个百分点"):
        return True
    if claim.bound in ("at_least", "at_most"):
        return True
    return claim.is_plan


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
    if claim.period_norm is None:
        # ⚠ **「没有期间」和「有期间但那一年的指标没数据」是两件事，
        #   而原来的文案把两者说成了一件。** 它写的是
        #   `f"{claim.period_norm or '目标期间'} 未披露 {metric}"`——
        #   期间为空时渲染成「目标期间 未披露 营业成本」，
        #   读的人会以为**这个指标整列没有数据**，而去查数据缺口。
        #   实测宝钢判成 unverifiable 的 161 条**全部**是这一类，
        #   而它们的指标**都有 11 条事实**——理由 100% 误诊。
        return _out(
            claim, metric, "unverifiable",
            f"这句话里没有可识别的期间，定位不到「哪一年」的数据来核对。"
            f"（判据指标是 {metric}）",
            confidence=0.0,
        )
    if current is None:
        return _out(
            claim, metric, "unverifiable",
            f"{claim.period_norm} 年未披露 {metric}，"
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

    # 「约 10%」「10% 左右」——没有公开容差，不擅自判完成与否。
    #
    # ⚠ **只对比例型目标在这里早退。** 绝对量目标即使带「约」，也还有
    # 单位换算与「原始计划偏差」要报（8-1 要求留痕、8-3 第 4 条允许展示），
    # 所以让它走 `_judge_absolute_target`，那里自己处理 about。
    # 不分开的话，一句「约 2,420 亿元」会连偏差都看不见——
    # 而偏差恰恰是这条链路唯一该给人看的东西。
    if claim.bound == "about" and claim.magnitude_unit in _RATIO_UNITS:
        return _out(
            claim, metric, "needs_review",
            f"原文表述为「{claim.magnitude_raw}」，属约数且无公开容差，"
            f"只展示实际偏差、不擅自认定完成或未完成。",
            confidence=0.0, magnitude_target=claim.magnitude_raw,
            fact_period=current.period,
        )

    # ⚠ 绝对量目标走另一条路：**换算 + 报偏差，但不进 H / C 的支持相悖判定**。
    # 口径见 `_judge_absolute_target`。
    if claim.magnitude_unit not in _RATIO_UNITS:
        return _judge_absolute_target(claim, current, metric)

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


def _judge_absolute_target(
    claim: ClaimInput, current: ActualValue, metric: str
) -> MatchOutcome:
    """绝对量目标的核验（会计口径 **8-1 / 8-2 / 8-3**，2026-10-01 答复）。

    ★ **这一条永远不出 supported / contradicted。** 不是「还没做」，
    是三问的回答合起来就定成这样：

        8-1  单位可以换算，1 亿元 = 100 百万元；原始数值、原始单位、
             换算因子、标准化数值四样都要留痕
        8-2  方向按 sign_convention 定：成本类实际不高于计划为达成，
             收入类实际不低于计划为达成；含「约 / 左右」的先标待核查
        8-3  第 3 条——只有总成本、没有可靠的成本结构或产品组合信息时，
             **不能按产量同比例缩放，也不能直接判「未达成」**，转人工复核
             第 4 条——未经调整的总额差异可以展示为「原始计划偏差」，
             **但不进入 H 的支持/相悖判定**

    所以这一层做的是：**把偏差按正确的单位算出来、把 8-2 的方向算出来
    给人看**，然后把 verdict 定在 needs_review。

    #### 这一段有来历

    在 8-1/8-2 定下来之前，这里只会说一句「口径未定」。而更早的版本
    干脆**没挡**，于是金额类指标的实测相对变化（0.0429）被拿去减绝对目标
    （2,420 亿元），偏差必然是 −2,419.96 —— **算术上任何绝对量目标都不可能达标**。
    那个「未达成」有公式、有偏差数字、有理由，和真结论长得一模一样；
    实测宝钢一次跑出 12 条，进 H 的 9 个观测里 7 个是这么来的。

    #### 为什么不是「换算完就直接判」

    8-3 第 3 条要的是「同口径、同范围、并按固定成本与变动成本做过产量调整」
    之后再比。宝钢 2018 那一句的原文是：

        2018年，宝钢股份计划产铁4563万吨、产钢4737万吨、销售商品坯材4568万吨、
        营业总收入2786亿元、营业成本2420亿元。

    实际成本 2,590.85 亿元高于计划 2,420 亿元，看着像未达成——但实际产量
    若也高于计划，成本高是自然的。**库里没有成本结构和产品组合信息**，
    做不了这个调整，所以按第 3 条标待核查。会计答复的末句原话：

        「因此，2018 年营业成本目标不应仅用 2,590.85 亿元对 2,420 亿元
          直接判定未达成；应先完成同口径、产量和成本结构复核。」

    #### 已知的残留：**「削减额」和「水平值」是两种量**

    年报里还有一类：

        成本环比削减30亿元以上；      2021年实现成本削减11.5亿元，超额完成年度目标

    那个 30 亿元是**削减额**（一个差），而财务事实库里的 `operating_cost`
    是**水平值**（2,809 亿元）。两者相减同样没有意义。

    当前数据下这一类**全部**被上面那道「数不是这个指标的」闸门挡住了
    （实测 12 条，`magnitude_metric_aligned` 全是 0）——因为句子里没有
    「营业成本」这个别名，抽取时走的是退回取数那条路。

    ⚠ **这是巧合，不是保证。** 哪天抽取把别名匹配做得更宽
    （比如把「成本削减」也算作营业成本的别名），这一批就会变成
    `aligned=1`，然后拿削减额去减水平值。要修得在抽取层区分
    「差额型」与「水平型」目标（`Magnitude` 再加一位），
    **不是在判定层补一个阈值**——补阈值只能盖住见过的量级。
    """
    target = claim.magnitude_value
    assert target is not None
    raw = claim.magnitude_raw or str(target)
    unit = claim.magnitude_unit or ""

    # ---- 8-3 第 1 条之先：这个数是不是这个指标的数 ----------------------
    # 抽取时若找不到主判据别名，会退回取「第一个带单位的数」。那意味着
    # 这句话里的数字与主判据**在文字上没有关联**。真实撞到过：
    #
    #     2024年，公司预算安排固定资产投资资金239.2亿元，主要用于……
    #
    # 主题映射成了 `operating_cost`，239.2 亿元其实是资本开支。拿它和
    # 营业成本（2,809 亿元）比，偏差 +280,626 百万元——按 8-2 的成本方向
    # 算出来还是「未达成」，**有数字、有理由、有公式**。
    #
    # ⚠ 这一条挡住的是「主张与指标的对应关系」这个更上游的错误，
    # 量纲那道闸门挡不住它（两边都是金额）。所以这一层连**偏差都不算**——
    # 算出来就会有人看，看了就会有人当真。
    if not claim.target_metric_aligned:
        return _out(
            claim, metric, "needs_review",
            f"绝对量目标「{raw}」在原文里**没有和主判据 {metric} 的别名相邻**，"
            f"是抽取时退回取的「第一个带单位的数」。两者很可能不是同一件事"
            f"（实测撞到过资本开支被当成营业成本目标），"
            f"按 8-3 第 1 条先核对业务范围与期间——**这一层连偏差都不算**。",
            confidence=0.0, magnitude_target=raw, fact_period=current.period,
        )

    # ---- 8-1 之先：量纲对不上就不能比 ----------------------------------
    # 「营业总收入较计划减少 298.46 亿元」这句被映射到钢材销量（吨）上过。
    # 两个不同量纲的数相减，算得出偏差也写得出理由——所以先挡量纲。
    # ⚠ 只在**两边都知道**、且确实不同时才挡：不知道不等于不匹配。
    unit_kind = _UNIT_KIND.get(unit)
    if claim.metric_unit_kind and unit_kind and unit_kind != claim.metric_unit_kind:
        return _out(
            claim, metric, "needs_review",
            f"绝对量目标「{raw}」的量纲是{_UNIT_KIND_CN.get(unit_kind, unit_kind)}，"
            f"而主判据 {metric} 是"
            f"{_UNIT_KIND_CN.get(claim.metric_unit_kind, claim.metric_unit_kind)}——"
            f"**两者不能相减**，偏差算得出来也没有意义。转人工复核。",
            confidence=0.0, magnitude_target=raw, fact_period=current.period,
        )

    # ---- 8-1 单位换算 ---------------------------------------------------
    factor = _MILLION_FACTOR.get(unit)
    if factor is None:
        return _out(
            claim, metric, "needs_review",
            f"绝对量目标「{raw}」的单位「{unit or '（无）'}」没有会计口径认可的"
            f"换算依据（8-1 只给了金额类：1 亿元 = 100 百万元）。"
            f"**不猜一个看起来合理的换算**，转人工复核。",
            confidence=0.0, magnitude_target=raw, target_unit=unit or None,
            fact_period=current.period,
        )
    target_millions = target * factor
    variance = current.value - target_millions
    variance_ratio = (
        variance / target_millions if target_millions != 0 else None
    )

    # ---- 8-2 达成方向（只算给人看，不决定 verdict）---------------------
    better = _SIGN_TO_BETTER.get(claim.metric_sign or "")
    if claim.bound == "about":
        reference = (
            f"原文是「{raw}」，含约数且无公开容差——按 8-2 先标待核查，"
            f"**不擅自认定完成或未完成**。"
        )
    elif better is None:
        reference = (
            f"主判据 {metric} 的 sign_convention 是"
            f"「{claim.metric_sign or '（缺）'}」，说明不了「大」还是「小」算好，"
            f"**方向判不了**，只报偏差。"
        )
    else:
        met = variance <= 0 if better == "lower" else variance >= 0
        who = "成本类" if better == "lower" else "收入 / 收益类"
        want = "不高" if better == "lower" else "不低"
        reference = (
            f"按 8-2 的方向（{who}，实际{want}于计划即为达成）："
            f"实际{'>' if variance > 0 else ('<' if variance < 0 else '=')}"
            f"计划，参考结论「{'达成' if met else '未达成'}」。"
        )

    # ---- 8-3 第 3 条 + 第 4 条：转人工复核，且不进 H -------------------
    reason = (
        f"绝对量目标「{raw}」按 8-1 换算：{target} {unit} × {factor} = "
        f"{target_millions} 百万元；实际 {current.value} 百万元，"
        f"原始计划偏差 {variance:+} 百万元"
        + (f"（{variance_ratio:+.2%}）" if variance_ratio is not None else "")
        + f"。{reference}"
        f"按 8-3 第 3 条，只有总成本、没有成本结构与产品组合信息时不得按产量"
        f"同比例缩放、也不得直接判未达成；第 4 条，未经调整的总额差异"
        f"**只作「原始计划偏差」展示，不进入 H 的支持/相悖判定**。"
        f"因此转人工复核，完成同口径与产量复核后才可计分。"
    )

    label = {"exact": "等于", "at_least": "不低于", "at_most": "不高于",
             "about": "约"}.get(claim.bound, claim.bound)
    return _out(
        claim, metric, "needs_review", reason,
        confidence=0.0,
        magnitude_target=raw,
        magnitude_actual=str(current.value),
        relative_deviation=variance_ratio.quantize(Decimal("0.000001"))
        if variance_ratio is not None else None,
        fact_period=current.period,
        target_unit=unit or None,
        target_millions=target_millions,
        unit_factor=factor,
        plan_variance=variance,
        plan_reference=reference,
        formula=(
            f"目标 {target} {unit} × {factor} = {target_millions} 百万元"
            f"（计划{label}此数）；实际 {current.period} = {current.value} 百万元；"
            f"原始计划偏差 = 实际 − 目标 = {variance:+} 百万元"
        ),
        inputs=(current.fact_id,),
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
    target_unit: str | None = None,
    target_millions: Decimal | None = None,
    unit_factor: Decimal | None = None,
    plan_variance: Decimal | None = None,
    plan_reference: str | None = None,
) -> MatchOutcome:
    return MatchOutcome(
        claim_id=claim.claim_id,
        metric_key=metric,
        claim_period=claim.period_norm or "",
        fact_period=fact_period or claim.period_norm or "",
        verdict=verdict,
        reason=_with_substitution_note(claim, reason),
        confidence=confidence,
        direction_claim=claim.direction,
        direction_actual=direction_actual,
        direction_consistent=direction_consistent,
        magnitude_target=magnitude_target,
        magnitude_actual=magnitude_actual,
        relative_deviation=relative_deviation,
        formula=formula,
        inputs=inputs,
        target_unit=target_unit,
        target_millions=target_millions,
        unit_factor=unit_factor,
        plan_variance=plan_variance,
        plan_reference=plan_reference,
    )


#: 回退判据的披露语。**写在唯一出口上**，所以每一条判定理由都会带上它——
#: 逐个 reason 字符串去补的话，漏掉一处就是一个「看起来用的是钢材销量」
#: 的假象，而两种理由长得完全一样。
_SUBSTITUTION_NOTE = (
    " ⚠ 本条的判据**不是**钢材销量：该公司年报只披露了行业聚合口径的销量"
    "（未说明是钢材还是粗钢），这里按会计 2026-10-06 的授权改用"
    " `industry_sales_volume`，**仅用于同口径方向判断**，"
    "不映射为通用钢材销量、不参与其他数量计算。"
)


def _with_substitution_note(claim: ClaimInput, reason: str) -> str:
    return reason + _SUBSTITUTION_NOTE if claim.metric_substituted else reason


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
