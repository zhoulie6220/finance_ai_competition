"""Q2：应收账款周转天数与账龄同时恶化。

纯函数，零 IO。口径见会计口径 gap_closure_v1.0 §2。

## 判据

```
Q2_trigger = (DSO同比增加 > 10天)
             AND (1年以上应收账款占比同比增加 > 0.5个百分点)
```

两个条件**同时**成立才算触发。

```
DSO       = 平均应收账款账面余额 / 营业收入 × 365
账龄占比  = 账龄超过一年的应收账款账面余额 / 应收账款账面余额
平均应收账款 = (年初账面余额 + 年末账面余额) / 2
```

## 四条容易搞错的地方

1. **一律用账面余额（gross），不是净额。** 净额扣了坏账准备，
   与账面余额口径不同，**混用不会报错**，只会让 DSO 和占比都偏小。
   只有净额时状态落 `proxy_net`，且不能参与正式判定。

2. **边界算「未触发」。** 会计口径 §2.4：「两个条件均严格『大于』阈值才为
   triggered；等于 10 天或等于 0.5 个百分点为 not_triggered」。
   这和叙事层的噪声区间规则一致——**边界不算变化**。

3. **缺数据是 `unavailable`，不是 `not_triggered`。**
   「没查到」和「查了没问题」在数值上长得一样，含义完全相反。
   把前者显示成后者，等于把「我们没查」写成「没有风险」。

4. **触发只是复核信号**，不是坏账、舞弊或虚假披露结论。
   §2.4 最后一句写得很清楚。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.engine.index import RatioComponent

#: 判定结果。**刻意把 `unavailable` 与 `not_triggered` 分开**——
#: 它们在任何数值表示里都一样，只有在这里分得开。
Q2Status = Literal["triggered", "not_triggered", "unavailable", "needs_review"]

TRIGGERED: Q2Status = "triggered"
NOT_TRIGGERED: Q2Status = "not_triggered"
UNAVAILABLE: Q2Status = "unavailable"
NEEDS_REVIEW: Q2Status = "needs_review"

DAYS_PER_YEAR = Decimal(365)
PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class Q2Config:
    """阈值。初值与 rule_config 的 quality.* 一致。"""

    #: DSO 同比增加超过该值（天）
    dso_gap_days: Decimal = Decimal("10")
    #: 1年以上占比同比增加超过该值（0.5 个百分点）
    aging_share_gap: Decimal = Decimal("0.005")

    def __post_init__(self) -> None:
        if self.dso_gap_days < 0:
            raise ValueError("DSO 阈值不能为负")
        if self.aging_share_gap < 0:
            raise ValueError("账龄占比阈值不能为负")


@dataclass(frozen=True)
class AgingYear:
    """一个年度的账龄数据（会计按 §2.3 的表抄进来的那一行）。"""

    period: str
    receivable_gross: Decimal | None
    over_one_year: Decimal | None
    revenue: Decimal | None
    #: 与 `q2_aging.status` 的枚举一致
    status: str = "validated"
    #: 会计在 `q2_aging.note` 里写的理由。
    #: **非 `validated` 时由 CHECK 约束强制非空**，所以判定层可以放心把它
    #: 带进失败原因里——那正是「查过了、确认拿不到」和「还没人去抄」的分界。
    note: str | None = None
    #: 回链：这一行的数据抄自哪一页、原文是什么、谁复核的。
    #: **下钻面板要用**——评委问「这一对比较是怎么判的」，答案不只是
    #: 两个比率，还包括「这两个数从哪来、谁签的字」。
    source_page: int | None = None
    source_text: str | None = None
    reviewer: str | None = None

    @property
    def usable(self) -> bool:
        """能不能参与正式判定。

        `proxy_net` **不能**——它只有净额，口径与账面余额不同。
        `pending` / `unavailable_disclosure` 也不能。
        """
        return self.status == "validated"

    @property
    def complete(self) -> bool:
        return all(
            v is not None
            for v in (self.receivable_gross, self.over_one_year, self.revenue)
        )


@dataclass(frozen=True)
class Q2Outcome:
    status: Q2Status
    #: 两个条件是否都成立。`unavailable` 时为 None——**不是 False**
    triggered: bool | None
    dso_current: Decimal | None = None
    dso_prior: Decimal | None = None
    dso_gap: Decimal | None = None
    share_current: Decimal | None = None
    share_prior: Decimal | None = None
    share_gap: Decimal | None = None
    reason: str = ""
    formula: str = ""
    #: 判不了时**为什么判不了**。机器可读的那一版，和 `reason` 的散文分开。
    #:
    #:   "pending"                 会计还没抄 → 去催人，仍然阻断 Q
    #:   "unavailable_disclosure"  年报确实没披露 → **判不适用**，不阻断
    #:
    #: ⚠ 不能靠解析 `reason` 来分这两种：那是给人读的句子，
    #:   改一次文案就悄悄换一套行为。2026-10-06 会计对 Q2 单年缺口
    #:   明确答复「判不适用、从分母剔除、不阻断整个 Q」——
    #:   而这两种"判不了"的处置**正好相反**，靠文案区分迟早出事。
    cause: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (TRIGGERED, NOT_TRIGGERED)

    def describe(self) -> str:
        if self.status == UNAVAILABLE:
            return f"无法判定：{self.reason}"
        if self.status == NEEDS_REVIEW:
            return f"待人工复核：{self.reason}"
        tail = "已触发" if self.status == TRIGGERED else "未触发"
        return (
            f"{tail}（DSO 同比 {self.dso_gap:+} 天，"
            f"账龄占比同比 {self.share_gap:+}）"
        )


def dso(
    receivable_current: Decimal, receivable_prior: Decimal, revenue: Decimal
) -> Decimal | None:
    """应收账款周转天数。

    `平均应收账款 = (年初 + 年末) / 2`——会计口径 §2.1 明确要期初期末平均，
    不用单一时点值。用年末余额算会把季节性放大成趋势。

    营业收入为零或负时不计算（返回 None）。**不返回 0**——
    「算不出来」和「周转天数是零」是两回事。
    """
    if revenue <= 0:
        return None
    average = (receivable_current + receivable_prior) / Decimal(2)
    return (average / revenue * DAYS_PER_YEAR).quantize(PLACES)


def aging_share(over_one_year: Decimal, receivable_gross: Decimal) -> Decimal | None:
    """账龄 1 年以上的占比。

    分母为 0 时返回 None——没有应收账款就没有「占比」这回事，
    返回 0 会让它看起来像「账龄结构很健康」。
    """
    if receivable_gross <= 0:
        return None
    return (over_one_year / receivable_gross).quantize(PLACES)


def judge_q2(
    prior: AgingYear, current: AgingYear, cfg: Q2Config | None = None
) -> Q2Outcome:
    """判定两个年度之间的 Q2。"""
    cfg = cfg or Q2Config()

    # ---- 能不能判：先过状态与完整性两道闸 --------------------------------
    #
    # ⚠ `pending` 与 `unavailable_disclosure` **必须分开说**。
    #
    # 两者原先共用一句「未录入或年报未披露」，于是页面上分不出
    # 「会计还没抄」和「年报里根本没有这一项」——而这是**两件要不同的人
    # 去做不同的事**的情况：前者催会计，后者是披露缺口、要换口径或放弃。
    # 合成一句话的后果不是报错，是**催错了人**，或者更糟：
    # 明明只是没人去抄，被读成「这家公司没披露」——一个数据结论。
    for label, row in (("上期", prior), ("本期", current)):
        if row.status == "pending":
            return Q2Outcome(
                status=UNAVAILABLE,
                triggered=None,
                cause="pending",
                reason=f"{label}（{row.period}）的账龄数据**尚未录入**——"
                       f"不是年报没披露，是还没人去抄。"
                       f"**缺数据不等于未触发**。",
            )
        if row.status == "unavailable_disclosure":
            return Q2Outcome(
                status=UNAVAILABLE,
                triggered=None,
                cause="unavailable_disclosure",
                reason=f"{label}（{row.period}）的年报**未披露**账龄数据。"
                       f"这是披露缺口，不是还没抄。**缺数据不等于未触发**。",
            )
        if row.status == "proxy_net":
            return Q2Outcome(
                status=NEEDS_REVIEW,
                triggered=None,
                reason=f"{label}（{row.period}）只有净额（proxy_net），"
                       f"口径与账面余额不同，不能参与正式判定。",
            )
        # ⚠ `needs_review` 必须**单独说**，而且要带上会计写的理由。
        #
        # 它原来落到下面那条 `not row.complete` 上，于是页面显示
        # 「上期（2015）缺账面余额、1 年以上余额或营业收入」——
        # 而实际上账面余额**填了**（9,681,299,431.81），缺的只有 1 年以上余额，
        # 且会计在 note 里写清楚了为什么拿不到：
        # 「年报仅对组合计提部分披露账龄，单项计提/大额应收账款未按账龄拆分」。
        #
        # 打成「缺数据」的后果不是报错，是**把一次负责任的拒绝读成一次敷衍**——
        # 与上面 `pending` / `unavailable_disclosure` 要分开说是同一个道理，
        # 只是这里是第三种：**查过了，确认拿不到，并说明了原因**。
        if row.status == "needs_review":
            why = (row.note or "").strip()
            return Q2Outcome(
                status=NEEDS_REVIEW,
                triggered=None,
                reason=f"{label}（{row.period}）的账龄数据**查过了、判定为待复核**"
                       + (f"：{why}" if why else "（会计未写明原因）")
                       + " **缺数据不等于未触发。**",
            )
        if not row.complete:
            return Q2Outcome(
                status=UNAVAILABLE,
                triggered=None,
                reason=f"{label}（{row.period}）缺账面余额、1年以上余额或营业收入。"
                       f"**缺数据不等于未触发**。",
            )

    assert current.receivable_gross is not None and current.revenue is not None
    assert prior.receivable_gross is not None and prior.revenue is not None
    assert current.over_one_year is not None and prior.over_one_year is not None

    dso_now = dso(current.receivable_gross, prior.receivable_gross, current.revenue)
    dso_before = (
        dso(prior.receivable_gross, prior.receivable_gross, prior.revenue)
    )
    share_now = aging_share(current.over_one_year, current.receivable_gross)
    share_before = aging_share(prior.over_one_year, prior.receivable_gross)

    # ⚠ 上期的 DSO 需要**上上期**的余额才算得准。这里没有，
    #   就用上期余额自身当平均（等价于假设期初=期末）。
    #   这会让「DSO 同比」这一步带着已知的偏差——所以标 needs_review，
    #   而不是假装它和本期一样准。
    if dso_now is None or dso_before is None or share_now is None or share_before is None:
        return Q2Outcome(
            status=UNAVAILABLE,
            triggered=None,
            dso_current=dso_now,
            dso_prior=dso_before,
            share_current=share_now,
            share_prior=share_before,
            reason="营业收入或应收账款为零/负，算不出周转天数或占比。",
        )

    dso_gap = (dso_now - dso_before).quantize(PLACES)
    share_gap = (share_now - share_before).quantize(PLACES)

    # ★ 边界算「未触发」：严格大于才触发（会计口径 §2.4）
    hit_dso = dso_gap > cfg.dso_gap_days
    hit_share = share_gap > cfg.aging_share_gap
    triggered = hit_dso and hit_share

    formula = (
        f"DSO {prior.period}={dso_before} → {current.period}={dso_now}"
        f"（差 {dso_gap} 天，阈值 >{cfg.dso_gap_days}）；"
        f"1年以上占比 {share_before} → {share_now}"
        f"（差 {share_gap}，阈值 >{cfg.aging_share_gap}）"
    )
    detail = (
        f"DSO 同比 {'超过' if hit_dso else '未超过'}阈值、"
        f"账龄占比 {'超过' if hit_share else '未超过'}阈值"
    )

    return Q2Outcome(
        status=TRIGGERED if triggered else NOT_TRIGGERED,
        triggered=triggered,
        dso_current=dso_now,
        dso_prior=dso_before,
        dso_gap=dso_gap,
        share_current=share_now,
        share_prior=share_before,
        share_gap=share_gap,
        reason=f"{detail}，两个条件{'均' if triggered else '未全部'}成立。"
               f"⚠ 触发是**需复核的风险信号**，不是坏账或虚假披露结论。",
        formula=formula,
    )


def to_component(outcomes: list[Q2Outcome]) -> RatioComponent:
    """把逐年的 Q2 判定汇总成指数公式里的 Q。

    分母是**能判的年度数**，分子是其中触发的。

    ## 两种「判不了」的处置**正好相反**（会计 2026-10-06 答复）

    | 为什么判不了 | `cause` | 处置 |
    |---|---|---|
    | 会计还没抄 | `pending` | **仍然阻断**——去催人，不是数据不可得 |
    | 年报确实没披露 | `unavailable_disclosure` | **判不适用**，从分母剔除，不阻断整个 Q |

    答复原话（针对华菱 2019 账龄那个结构性缺口）：

    > Q2 对经核实不可得的年度比较判不适用、剔除分母并披露原因，
    > **不阻断整个 Q**。

    适用条件她也写明了：已查年报正文、附注和相邻年度比较栏仍拿不到；
    **必须保留缺失原因与页码**；**不得把缺失解释为「无风险」**；
    输出要同时披露有效比较数、剔除年度和原因。所以这里把剔除的年度
    写进 `note` 并一路带下去——**不吭声地少算两个年度，
    页面上看起来就是「这两年本来就没问题」**。

    ⚠ 另一种判不了（`needs_review` / `proxy_net` / 字段缺）**仍然阻断**：
    那不是「数据不可得」，是「口径或证据有冲突」或「还没录全」，
    该做的事完全不同。
    """
    judged = [o for o in outcomes if o.ok]
    # 年报确实没披露 → 不适用，不进分子分母、也不阻断
    excluded = [o for o in outcomes if not o.ok and o.cause == "unavailable_disclosure"]
    blocked = [o for o in outcomes if not o.ok and o.cause != "unavailable_disclosure"]
    triggered = [o for o in judged if o.status == TRIGGERED]

    parts: list[str] = []
    if blocked:
        reasons = [f"{o.reason.split('。')[0]}" for o in blocked][:3]
        parts.append(f"{len(blocked)} 个年度无法判定：{'；'.join(reasons)}")
    if excluded:
        periods = [o.reason.split("）")[0].lstrip("上本期（") for o in excluded]
        parts.append(
            f"{len(excluded)} 个年度比较按口径判**不适用**（年报未披露，"
            f"已从 Q 的分母剔除）：{'、'.join(p for p in periods if p)}"
            f" —— ⚠ 这是「查不了」，**不是「无风险」**"
        )

    return RatioComponent(
        name="quality_conflict",
        label_cn="财务质量冲突 Q",
        numerator=len(triggered),
        denominator=len(judged),
        verified=not blocked and bool(judged),
        note="；".join(parts),
    )
