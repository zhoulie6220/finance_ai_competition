"""Q 项的财务质量检查清单（会计口径 v1.1 §A.3）。

纯函数，零 IO。

初赛固定三项：

    Q1  连续两个完整年度经营现金流净额为负，且这两个年度净利润均为正
    Q2  应收账款周转天数同比增加 > 10 天，**且**账龄 > 1 年的应收占比增加 > 0.5 个百分点
    Q3  存货周转天数同比增加 > 10 天，**且**同口径钢材销量下降 > 1%

> 这三条是**需复核的风险筛查条件，不是造假判据**。触发只表示该查一查。

## 最要紧的一条：未核完 ≠ 未触发

Q 的分母是「**已完成核验**的适用检查项数」。适用项没核完则 Q 不完整，
闸门不过、不出分——**不得把「未核验」当作「未触发」**。

本批数据里 Q2 必然走到这里：账龄超过一年的应收占比需要按账龄分段的
应收账款明细，而 743 条事实里没有这个数据源。系统的做法是如实标
`verified=False` 并写明原因，**不拿代理指标顶替**——那会把
「我们没查」变成「没问题」，而这两者在页面上长得一模一样。

## 去重

同一事项若已在 H / C 中扣分，Q 不再重复扣分：保留该检查的已核验分母，
重复事项不进入 Q 分子，并记录去重原因。同一件事被罚两次会让指数
凭空低一截，而且看不出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.engine.index import RatioComponent

# 判定为触发的阈值。初值与 rule_config 的 quality.* 一致。
TURNOVER_DAYS_GAP = Decimal("10")      # 周转天数同比增加超过该值
AGING_SHARE_GAP = Decimal("0.005")     # 账龄>1年应收占比增加超过该值（0.5 个百分点）
SALES_VOLUME_DECLINE = Decimal("0.01")  # 同口径销量下降超过该值（1%）
MIN_BALANCE_YEARS = 2                  # Q1 要求连续满足的年度数

DAYS_PER_YEAR = Decimal(365)


@dataclass(frozen=True)
class QualityItem:
    """一项检查的核验结果。

    `applicable` 与 `verified` 是**两个独立的维度**，不能合并：
      · applicable=False        这项不适用于本公司
      · applicable=True, verified=False   适用但还没查完
    后者正是「不得当作未触发」所指的情形。合并成一个字段就分不出来了。
    """

    key: str
    label_cn: str
    applicable: bool
    verified: bool
    triggered: bool | None
    detail: str
    note: str = ""

    @property
    def unresolved(self) -> bool:
        """适用但未核完。"""
        return self.applicable and not self.verified


@dataclass
class QualityChecklist:
    """三项检查的汇总。"""

    items: tuple[QualityItem, ...]
    deduped: tuple[str, ...] = field(default_factory=tuple)

    @property
    def applicable(self) -> tuple[QualityItem, ...]:
        return tuple(i for i in self.items if i.applicable)

    @property
    def all_verified(self) -> bool:
        """适用项全部核验完成。**未核完就不算完成。**"""
        return all(i.verified for i in self.applicable)

    @property
    def triggered(self) -> tuple[QualityItem, ...]:
        return tuple(i for i in self.applicable if i.verified and i.triggered)

    def to_component(self) -> RatioComponent:
        """转成指数公式里的 Q。

        ⚠ 分母是**已完成核验的适用项数**——不是全部适用项。
        分子是其中已确认触发的、且未在 H/C 中重复扣分的项数。
        """
        verified = [i for i in self.applicable if i.verified]
        counted = [i for i in verified if i.triggered and i.key not in self.deduped]
        note = ""
        if not self.all_verified:
            unresolved = [i.label_cn for i in self.applicable if not i.verified]
            note = f"适用项未核完：{'、'.join(unresolved)}"
        if self.deduped:
            extra = f"已在 H/C 中扣分，Q 不重复计：{'、'.join(self.deduped)}"
            note = f"{note}；{extra}" if note else extra
        return RatioComponent(
            name="quality_conflict",
            label_cn="财务质量冲突 Q",
            numerator=len(counted),
            denominator=len(verified),
            verified=self.all_verified,
            note=note,
        )

    def describe(self) -> str:
        lines = []
        for i in self.items:
            if not i.applicable:
                # ⚠ 「不适用」也必须带上理由。会计 2026-10-06 对 Q3 的答复要求
                #   「在覆盖率中**单独披露**，不能把缺失当作无冲突」——
                #   只印一个「不适用」，读的人分不出是「本来就不适用」
                #   还是「数据缺所以跳过了」，而这两件事含义相反。
                lines.append(
                    f"· {i.label_cn}：不适用" + (f"（{i.note}）" if i.note else "")
                )
            elif not i.verified:
                lines.append(f"· {i.label_cn}：**未核验完成**（{i.note}）")
            else:
                mark = "已触发" if i.triggered else "未触发"
                lines.append(f"· {i.label_cn}：{mark} —— {i.detail}")
        return "\n".join(lines)


# ---------------------------------------------------------------- 三项检查


def check_cfo_vs_net_income(
    *,
    cfo_by_year: dict[str, Decimal],
    net_income_by_year: dict[str, Decimal],
    years: tuple[str, ...],
) -> QualityItem:
    """Q1：连续两年经营现金流为负，而净利润为正。

    这个组合值得看一眼：账面盈利但收不回现金，可能是收入确认激进，
    也可能是营运资本被占用——**但它本身不是造假证据**，
    只是「该查一查」的入口。
    """
    usable = [y for y in years if y in cfo_by_year and y in net_income_by_year]
    if len(usable) < MIN_BALANCE_YEARS:
        return QualityItem(
            key="cfo_negative_ni_positive",
            label_cn="Q1 连续两年现金流为负而净利润为正",
            applicable=True,
            verified=False,
            triggered=None,
            detail="",
            note=f"只有 {len(usable)} 个年度同时有经营现金流与净利润数据，"
                 f"不足以判断连续两年",
        )

    streak = 0
    worst = 0
    for y in sorted(usable):
        if cfo_by_year[y] < 0 and net_income_by_year[y] > 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0

    triggered = worst >= MIN_BALANCE_YEARS
    return QualityItem(
        key="cfo_negative_ni_positive",
        label_cn="Q1 连续两年现金流为负而净利润为正",
        applicable=True,
        verified=True,
        triggered=triggered,
        detail=(
            f"最长连续 {worst} 个年度满足「经营现金流为负且净利润为正」"
            + ("（已触发）" if triggered else "")
        ),
    )


def check_receivable_aging(
    *,
    receivable_days_gap: Decimal | None,
    aging_share_gap: Decimal | None,
) -> QualityItem:
    """Q2：应收周转天数增加 > 10 天，**且**长账龄占比增加 > 0.5 个百分点。

    ⚠ 本批数据**必然未核验完成**：账龄超过一年的应收占比需要按账龄分段的
    应收账款明细，743 条事实里没有这个数据源。

    按 v1.1「适用项未核完则 Q 不完整，不得当作未触发」，这里如实标
    verified=False 并写明缺什么——**不拿代理指标顶替**。
    """
    if aging_share_gap is None:
        return QualityItem(
            key="receivable_days_and_aging",
            label_cn="Q2 应收周转天数与账龄同时恶化",
            applicable=True,
            verified=False,
            triggered=None,
            detail="",
            note=(
                "缺账龄超过一年的应收账款占比——这需要按账龄分段的应收明细，"
                "本批数据的字段字典里没有对应指标。按 v1.1 不得当作未触发。"
            ),
        )

    verified = receivable_days_gap is not None
    if not verified:
        return QualityItem(
            key="receivable_days_and_aging",
            label_cn="Q2 应收周转天数与账龄同时恶化",
            applicable=True,
            verified=False,
            triggered=None,
            detail="",
            note="缺应收周转天数（需要两期应收账款与营业收入）",
        )

    triggered = (
        receivable_days_gap > TURNOVER_DAYS_GAP and aging_share_gap > AGING_SHARE_GAP
    )
    return QualityItem(
        key="receivable_days_and_aging",
        label_cn="Q2 应收周转天数与账龄同时恶化",
        applicable=True,
        verified=True,
        triggered=triggered,
        detail=(
            f"周转天数同比 {receivable_days_gap:+} 天，"
            f"长账龄占比同比 {aging_share_gap:+}"
        ),
    )


def check_inventory_vs_sales(
    *,
    inventory_days_gap: Decimal | None,
    sales_volume_change: Decimal | None,
) -> QualityItem:
    """Q3：存货周转天数增加 > 10 天，**且**同口径钢材销量下降 > 1%。

    两个条件必须**同时**成立。只看存货积压可能是为旺季备货；
    只看销量下降可能是主动减产保价。两者同时恶化才值得看。

    ## 两种「算不出来」是两件事（会计 2026-10-06 答复，选项 C）

    这个函数里有两个输入，缺哪一个都判不了，但**缺的原因不同、该做的事也不同**：

    | 缺什么 | 为什么缺 | 判成 |
    |---|---|---|
    | 存货周转天数 | **我们的数据缺口**（存货/营业成本没采到） | `applicable=True, verified=False` → 去补数据 |
    | 同口径钢材销量 | **口径上不可得**——会计已裁定华菱/首钢的聚合行不映射进该字段 | **`applicable=False`（不适用）** |

    答复原话：

    > Q3 在同口径销量不可得时标记为"不适用"，不阻断 Q 完整性；
    > 同时从 Q3 分母剔除，并在覆盖率中单独披露，**不能把缺失当作"无冲突"**。

    所以 `applicable=False` 让它既不进分子也不进分母（Q 完整性不再被它钉死），
    而那句披露写进 `note` 并一路流到页面——**「不适用」不等于「未触发」**，
    这两件事在页面上必须看得出区别。
    """
    missing = []
    if inventory_days_gap is None:
        missing.append("存货周转天数（需要两期存货与营业成本）")
    if missing:
        return QualityItem(
            key="inventory_days_and_sales_volume",
            label_cn="Q3 存货周转天数上升且同口径销量下降",
            applicable=True,
            verified=False,
            triggered=None,
            detail="",
            note="缺" + "、".join(missing),
        )

    if sales_volume_change is None:
        return QualityItem(
            key="inventory_days_and_sales_volume",
            label_cn="Q3 存货周转天数上升且同口径销量下降",
            applicable=False,
            verified=True,
            triggered=None,
            detail="",
            note=(
                "该公司无**同口径**钢材销量（年报只给了行业聚合口径，"
                "会计 2026-10-06 裁定不映射进该字段），按口径本项判**不适用**，"
                "不计入 Q 的分子与分母。"
                "⚠ 这是「查不了」，**不是「没触发」**——不能当成财务质量无冲突。"
            ),
        )

    triggered = (
        inventory_days_gap > TURNOVER_DAYS_GAP
        and sales_volume_change < -SALES_VOLUME_DECLINE
    )
    return QualityItem(
        key="inventory_days_and_sales_volume",
        label_cn="Q3 存货周转天数上升且同口径销量下降",
        applicable=True,
        verified=True,
        triggered=triggered,
        # ⚠ 比率要**取整到 4 位**再印。`sales_volume_change` 是 Decimal 除法
        #   的原始结果，直接 f-string 会印出
        #   `-0.005973025048169556840077071291` —— 一整行数字，
        #   而且这个字符串会一路流到页面上。
        detail=(
            f"存货周转天数同比 {inventory_days_gap:+} 天，"
            f"销量同比 {sales_volume_change.quantize(Decimal('0.0001')):+}"
        ),
    )


def turnover_days(
    balance_current: Decimal, balance_prior: Decimal, flow: Decimal
) -> Decimal | None:
    """周转天数 = 平均余额 / 流量 × 365。

    取**期初期末平均**而不是单一时点值——用年末余额算周转天数会把
    季节性放大成趋势。流量为零或负时不计算（返回 None），
    不返回一个看起来像样的数字。
    """
    if flow <= 0:
        return None
    average = (balance_current + balance_prior) / Decimal(2)
    return (average / flow * DAYS_PER_YEAR).quantize(Decimal("0.01"))


def build_checklist(
    *,
    cfo_by_year: dict[str, Decimal],
    net_income_by_year: dict[str, Decimal],
    years: tuple[str, ...],
    receivable_days_gap: Decimal | None = None,
    aging_share_gap: Decimal | None = None,
    inventory_days_gap: Decimal | None = None,
    sales_volume_change: Decimal | None = None,
    deduped: tuple[str, ...] = (),
    receivable_aging_handled_elsewhere: bool = False,
) -> QualityChecklist:
    """跑完三项检查。

    `receivable_aging_handled_elsewhere=True` 表示 Q2 由
    `app/engine/q2.py::judge_q2` 那一路真实判定（用的是会计逐条抄录的账龄数据），
    本函数里这一项只作**占位**。

    ⚠ **占位项必须标成「不适用」，不能只是喂 None。** 这是 2026-10-06 修的一个
    真 bug，而且它藏了很久：

        调用方写的是 `receivable_days_gap=None, aging_share_gap=None`
        （注释「归 Q2 管，不重复计」），于是 `check_receivable_aging` 返回
        `applicable=True, verified=False` 的一项；
        `QualityChecklist.all_verified` 要求**所有适用项**都 verified
        → 恒为 False → `to_component().verified` 恒为 False
        → **Q 永远算不出来，不管账龄数据抄得多完整。**

    之前没人发现，是因为在它修掉之前 Q 本来就因缺账龄数据算不出来——
    「占位项恒不过」和「真的缺数据」在页面上**一模一样**：
    都是「Q 不可算或未核验完成」。修完这一处，Q 的可用性才真正由数据决定。
    """
    aging_item = (
        _receivable_aging_delegated()
        if receivable_aging_handled_elsewhere
        else check_receivable_aging(
            receivable_days_gap=receivable_days_gap, aging_share_gap=aging_share_gap
        )
    )
    items = (
        check_cfo_vs_net_income(
            cfo_by_year=cfo_by_year, net_income_by_year=net_income_by_year, years=years
        ),
        aging_item,
        check_inventory_vs_sales(
            inventory_days_gap=inventory_days_gap, sales_volume_change=sales_volume_change
        ),
    )
    return QualityChecklist(items=items, deduped=deduped)


def _receivable_aging_delegated() -> QualityItem:
    """Q2 的占位项：由 `app/engine/q2.py` 判定，这里标「不适用」。

    `applicable=False` 让它既不进分子分母、也不参与 `all_verified`——
    因为它和 `judge_q2` 判的是**同一件事**，两处各算一遍的话，
    汇总出来的 Q 里同一个风险会被数两次，而两个数都算得出来。
    """
    return QualityItem(
        key="receivable_days_and_aging",
        label_cn="Q2 应收周转天数与账龄同时恶化",
        applicable=False,
        verified=True,
        triggered=None,
        detail="",
        note="由 app/engine/q2.py::judge_q2 判定（用会计抄录的账龄数据），此处不重复计",
    )
