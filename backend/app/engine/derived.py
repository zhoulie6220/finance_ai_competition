"""派生指标：**由引擎按公式算出来**、而不是从年报里抄下来的那些字段。

字段字典里 `is_derived = 1` 的 21 个字段归这里管。它们的值在年报上**不存在**
——`毛利率` 不是印在报表上的一个数，是「毛利 ÷ 营业收入」算出来的。

## 三条边界

**一、公式的唯一真源是字段字典的 `note`。**
每一条的算式都抄自 `metric_definition.note`（那批注释是会计逐条审校过的），
本模块**不自己发明口径**。字典里没写公式的（`steel_spread`、
`nonrecurring_share`、`net_margin`）按最直白的定义写，并在 `SPECS` 里注明
「字典未给公式」。

**二、原始值不是年报原文，所以必须能点回它的两行。**
`source_text` 那一套在派生值上不成立——「毛利率 5.82%」在年报里找不到出处。
它的出处是**参与计算的那几行**，所以 `DerivedResult.sources` 给的是
`(字段, 期间)` 的列表，界面上点开显示的是算式与每行自己的原文页。
CLAUDE.md 里「派生值的出处是两行不是一行」说的就是这件事。

**三、拒绝优于猜测。** 缺任何一个输入都返回 `refused`，**不插补、不当 0**。
尤其不能拿「代理指标」顶上：`吨钢毛利` 的分子如果是「营业收入 − 营业成本」
而分母是钢材销量，那算出来的是**披露范围内**的吨钢毛利，
不能悄悄当成公司全部钢材的。口径不一致的地方一律拒绝，理由写清楚。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Callable, Mapping

PLACES = Decimal("0.000001")
#: 比率一律存成百分数的分子部分（`12.34` 表示 12.34%），与 `unit='%'` 配套。
#: 与字段字典 `unit_kind='percent'` 的读法一致。
PCT = Decimal("100")


@dataclass(frozen=True)
class SourceRef:
    """参与计算的一个输入。**派生值的出处是这几个，不是某一句原文。**"""

    metric_key: str
    period: str


@dataclass(frozen=True)
class DerivedResult:
    value: Decimal | None
    formula: str
    inputs: dict[str, str]
    sources: tuple[SourceRef, ...] = ()
    unit: str = "百万元"
    #: 非空即拒绝。**拒绝时 `value` 一定是 None**——与 `RatioResult` 同一条约定。
    refused: str | None = None

    @property
    def ok(self) -> bool:
        return self.refused is None


def _refuse(reason: str, formula: str, **inputs: str) -> DerivedResult:
    return DerivedResult(
        value=None, formula=formula, inputs=inputs, refused=reason
    )


#: 取值函数：`(metric_key, period) -> Decimal | None`。
#: 只喂**已验证且可比**的金额进来——由调用方（仓储层）保证。
Lookup = Callable[[str, str], Decimal | None]


def _get(lookup: Lookup, metric: str, period: str) -> Decimal | None:
    return lookup(metric, period)


@dataclass(frozen=True)
class Derivation:
    """一条派生规则。

    `compute` 只做「缺不缺、算不算、怎么算」，不含任何 IO，
    所以它可以被单独测——喂几个数进去断言结果，不需要建库。
    """

    metric_key: str
    label_cn: str
    unit: str
    #: 参与计算的字段。用来给界面标「点开看哪几行」，也是拒绝时的依据。
    inputs: tuple[str, ...]
    #: 是否需要上一期的值（`roe` 要平均净资产）。
    needs_prior: bool
    compute: Callable[[Lookup, str, str], DerivedResult]
    #: True 表示算式不是字典里给的，是我们按最直白的定义写的。
    formula_from_us: bool = False


# ---------------------------------------------------------------- 各条算式


def _diff(lookup: Lookup, period: str, *, name: str, a: str, b: str,
          labels: tuple[str, str]) -> DerivedResult:
    """A − B。两条基础科目相减，是全表里最没有口径争议的一类。"""
    formula = f"{name} = {labels[0]} − {labels[1]}"
    inputs = {labels[0]: str(va := _get(lookup, a, period)),
              labels[1]: str(vb := _get(lookup, b, period))}
    if va is None or vb is None:
        return _refuse(f"缺 {labels[0] if va is None else labels[1]}，不插补", formula, **inputs)
    return DerivedResult(
        value=(va - vb).quantize(PLACES), formula=formula, inputs=inputs,
        sources=(SourceRef(a, period), SourceRef(b, period)),
    )


def _ratio(lookup: Lookup, period: str, *, name: str, num: str, den: str,
           labels: tuple[str, str]) -> DerivedResult:
    """分子 ÷ 分母 × 100（百分数）。分母为 0 或缺失一律拒绝。"""
    formula = f"{name} = {labels[0]} ÷ {labels[1]} × 100"
    inputs = {labels[0]: str(vn := _get(lookup, num, period)),
              labels[1]: str(vd := _get(lookup, den, period))}
    if vn is None or vd is None:
        return _refuse(f"缺 {labels[0] if vn is None else labels[1]}，不插补", formula, **inputs)
    if vd == 0:
        return _refuse("分母为 0，比率无定义", formula, **inputs)
    try:
        value = (vn / vd * PCT).quantize(PLACES)
    except (DivisionByZero, InvalidOperation):  # pragma: no cover —— 上面已挡
        return _refuse("计算失败", formula, **inputs)
    return DerivedResult(
        value=value, formula=formula, inputs=inputs, unit="%",
        sources=(SourceRef(num, period), SourceRef(den, period)),
    )


def _sum_then(lookup: Lookup, period: str, *, name: str, parts: tuple[str, ...],
              labels: tuple[str, ...], then: Callable[[Decimal], DerivedResult],
              ) -> DerivedResult:
    """先求若干科目的和，再把和交给 `then`。"""
    vals = [_get(lookup, p, period) for p in parts]
    inputs = dict(zip(labels, (str(v) for v in vals)))
    missing = [lab for lab, v in zip(labels, vals) if v is None]
    if missing:
        return _refuse("缺 " + "、".join(missing) + "，不插补",
                       f"{name} = " + " + ".join(labels), **inputs)
    total = sum(vals, Decimal(0))  # type: ignore[arg-type]
    return then(total)


# ---------------------------------------------------------------- 具体规则


def _gross_profit(lk: Lookup, p: str, _: str) -> DerivedResult:
    return _diff(lk, p, name="毛利", a="revenue", b="operating_cost",
                 labels=("营业收入", "营业成本"))


def _ebit(lk: Lookup, p: str, _: str) -> DerivedResult:
    """`利润总额 + 利息费用 − 利息收入`（会计口径 §8.1 的 Reported EBIT）。

    ⚠ 口径里 EBIT 有**三套**并存（reported / cross-check / adjusted），
    主 DCF 用 reported。本字段就是 reported 那一套，另外两套在
    `normalization` 那边。**不要把 cross-check 的算式搬到这里**——
    两者在有巨额汇兑损益的年份能差出十几个百分点。
    """
    a, b, c = (_get(lk, k, p) for k in
               ("profit_before_tax", "interest_expense", "interest_income"))
    formula = "息税前利润 = 利润总额 + 利息费用 − 利息收入"
    inputs = {"利润总额": str(a), "利息费用": str(b), "利息收入": str(c)}
    if a is None or b is None or c is None:
        lack = "利润总额" if a is None else ("利息费用" if b is None else "利息收入")
        return _refuse(f"缺 {lack}，不插补", formula, **inputs)
    return DerivedResult(
        value=(a + b - c).quantize(PLACES), formula=formula, inputs=inputs,
        sources=(SourceRef("profit_before_tax", p),
                 SourceRef("interest_expense", p),
                 SourceRef("interest_income", p)),
    )


def _ebitda(lk: Lookup, p: str, prior: str) -> DerivedResult:
    """`EBIT + 折旧摊销`。折旧摊销取现金流量表补充资料的三行。

    ⚠ 三行是**相加**不是取一行：固定资产折旧、无形资产摊销、
    使用权资产折旧是三件事（会计 2026-10-06 答复明确「不并入、
    也不互相冒充」）。少任何一行都拒绝——只加其中两行会得到一个
    偏小的 EBITDA，而它在页面上和完整版长得一模一样。
    """
    base = _ebit(lk, p, prior)
    if not base.ok:
        return base
    parts = ("depreciation_amortization", "amortization_intangible",
             "right_of_use_asset_depreciation")
    labels = ("固定资产折旧", "无形资产摊销", "使用权资产折旧")
    vals = [_get(lk, k, p) for k in parts]
    inputs = {**base.inputs, **dict(zip(labels, (str(v) for v in vals)))}
    formula = "息税折旧摊销前利润 = EBIT + 固定资产折旧 + 无形资产摊销 + 使用权资产折旧"
    missing = [lab for lab, v in zip(labels, vals) if v is None]
    if missing:
        return _refuse("缺 " + "、".join(missing) + "，不插补", formula, **inputs)
    total = base.value + sum(vals, Decimal(0))  # type: ignore[operator,arg-type]
    return DerivedResult(
        value=total.quantize(PLACES), formula=formula, inputs=inputs,
        sources=base.sources + tuple(SourceRef(k, p) for k in parts),
    )


def _roe(lk: Lookup, p: str, prior: str) -> DerivedResult:
    """`净利润 ÷ 平均归母权益 × 100`。

    字典的 `note` 写的是「平均净资产口径」，所以分母是**期初期末平均**：
    `(本期权益 + 上期权益) / 2`。第一年没有上期 → **拒绝**，
    不拿期末权益顶替（那样算出来的 ROE 会偏高，且看不出是哪一种口径）。
    """
    num, cur, prev = (_get(lk, k, pp) for k, pp in
                      (("net_income_parent", p), ("equity_parent", p),
                       ("equity_parent", prior)))
    formula = "净资产收益率 = 归母净利润 ÷ ((本期归母权益 + 上期归母权益) ÷ 2) × 100"
    inputs = {"归母净利润": str(num), f"{p} 年归母权益": str(cur),
              f"{prior} 年归母权益": str(prev)}
    if num is None or cur is None or prev is None:
        lack = ("归母净利润" if num is None else
                (f"{p} 年归母权益" if cur is None else f"{prior} 年归母权益"))
        return _refuse(f"缺 {lack}（平均净资产口径要两期），不插补", formula, **inputs)
    avg = (cur + prev) / 2
    if avg == 0:
        return _refuse("平均净资产为 0，比率无定义", formula, **inputs)
    return DerivedResult(
        value=(num / avg * PCT).quantize(PLACES), formula=formula,
        inputs=inputs, unit="%",
        sources=(SourceRef("net_income_parent", p),
                 SourceRef("equity_parent", p), SourceRef("equity_parent", prior)),
    )


def _gross_margin(lk: Lookup, p: str, prior: str) -> DerivedResult:
    gp = _gross_profit(lk, p, prior)
    if not gp.ok:
        return gp
    rev = _get(lk, "revenue", p)
    formula = "毛利率 = 毛利 ÷ 营业收入 × 100"
    inputs = {**gp.inputs, "营业收入": str(rev)}
    if rev is None:
        return _refuse("缺 营业收入，不插补", formula, **inputs)
    if rev == 0:
        return _refuse("营业收入为 0，比率无定义", formula, **inputs)
    return DerivedResult(
        value=(gp.value / rev * PCT).quantize(PLACES),  # type: ignore[operator]
        formula=formula, inputs=inputs, unit="%",
        # 出处 = 毛利那两行 + 营业收入。营业收入与毛利的被减数**是同一行**，
        # 但这个函数不去重——去重要按 (字段, 期间) 做，交给上层统一处理更稳。
        sources=gp.sources + (SourceRef("revenue", p),),
    )


def _net_margin(lk: Lookup, p: str, _: str) -> DerivedResult:
    return _ratio(lk, p, name="净利率", num="net_income", den="revenue",
                  labels=("净利润", "营业收入"))


def _cash_conversion(lk: Lookup, p: str, _: str) -> DerivedResult:
    return _ratio(lk, p, name="现金转化率", num="cfo", den="net_income",
                  labels=("经营活动现金流净额", "净利润"))


def _nonrecurring_share(lk: Lookup, p: str, _: str) -> DerivedResult:
    return _ratio(lk, p, name="非经常性损益占比", num="non_recurring_gain_loss",
                  labels=("非经常性损益", "净利润"), den="net_income")


def _ebit_margin(lk: Lookup, p: str, prior: str) -> DerivedResult:
    e = _ebit(lk, p, prior)
    if not e.ok:
        return e
    rev = _get(lk, "revenue", p)
    formula = "EBIT 利润率 = EBIT ÷ 营业收入 × 100"
    inputs = {**e.inputs, "营业收入": str(rev)}
    if rev is None:
        return _refuse("缺 营业收入，不插补", formula, **inputs)
    if rev == 0:
        return _refuse("营业收入为 0，比率无定义", formula, **inputs)
    return DerivedResult(value=(e.value / rev * PCT).quantize(PLACES),  # type: ignore[operator]
                         formula=formula, inputs=inputs, unit="%",
                         sources=e.sources + (SourceRef("revenue", p),))


def _ebitda_margin(lk: Lookup, p: str, prior: str) -> DerivedResult:
    e = _ebitda(lk, p, prior)
    if not e.ok:
        return e
    rev = _get(lk, "revenue", p)
    formula = "EBITDA 利润率 = EBITDA ÷ 营业收入 × 100"
    inputs = {**e.inputs, "营业收入": str(rev)}
    if rev is None:
        return _refuse("缺 营业收入，不插补", formula, **inputs)
    if rev == 0:
        return _refuse("营业收入为 0，比率无定义", formula, **inputs)
    return DerivedResult(value=(e.value / rev * PCT).quantize(PLACES),  # type: ignore[operator]
                         formula=formula, inputs=inputs, unit="%",
                         sources=e.sources + (SourceRef("revenue", p),))


def _interest_bearing_debt(lk: Lookup, p: str, _: str) -> DerivedResult:
    """字典的定义：`短期借款 + 长期借款 + 应付债券 + 租赁负债 + 一年内到期的非流动负债`。

    ⚠ **最后一项字段字典里没有**，所以现在恒拒绝。这是「拒绝优于猜测」的
    正面例子：少加「一年内到期的非流动负债」会得到一个偏小的有息负债，
    而它和完整口径在页面上长得一模一样。要么补字段，要么这条先空着。
    """
    parts = ("short_term_borrowing", "long_term_borrowing", "bonds_payable",
             "lease_liability", "non_current_liabilities_due_within_one_year")
    labels = ("短期借款", "长期借款", "应付债券", "租赁负债", "一年内到期的非流动负债")
    return _sum_then(lk, p, name="有息负债合计", parts=parts, labels=labels,
                     then=lambda total: DerivedResult(
                         value=total.quantize(PLACES),
                         formula="有息负债合计 = " + " + ".join(labels),
                         inputs={}, sources=tuple(SourceRef(k, p) for k in parts)))


def _operating_working_capital(lk: Lookup, p: str, _: str) -> DerivedResult:
    """字典的定义：`(应收账款 + 应收票据 + 存货) − (应付账款 + 应付票据 + 合同负债)`。

    ⚠ **「应付票据」字段字典里没有**，所以现在恒拒绝（理由同上）。
    """
    assets = ("accounts_receivable", "notes_receivable", "inventory")
    alabels = ("应收账款", "应收票据", "存货")
    liabs = ("accounts_payable", "notes_payable", "contract_liabilities")
    llabels = ("应付账款", "应付票据", "合同负债")
    av = [_get(lk, k, p) for k in assets]
    lv = [_get(lk, k, p) for k in liabs]
    inputs = dict(zip(alabels + llabels, (str(v) for v in av + lv)))
    formula = ("经营性营运资本 = (应收账款 + 应收票据 + 存货) "
               "− (应付账款 + 应付票据 + 合同负债)")
    missing = [lab for lab, v in zip(alabels + llabels, av + lv) if v is None]
    if missing:
        return _refuse("缺 " + "、".join(missing) + "，不插补", formula, **inputs)
    total = sum(av, Decimal(0)) - sum(lv, Decimal(0))  # type: ignore[arg-type]
    return DerivedResult(value=total.quantize(PLACES), formula=formula, inputs=inputs,
                         sources=tuple(SourceRef(k, p) for k in assets + liabs))


def _net_debt(lk: Lookup, p: str, prior: str) -> DerivedResult:
    """字典的定义：`有息负债 − 非经营性现金`。

    ⚠ 「非经营性现金」不是一个能直接取到的字段（货币资金里既有经营性的
    也有非经营性的）。拿「货币资金」顶替会得到一个偏小的净债务，
    **而且看不出用了哪个口径**。所以这条依赖 `interest_bearing_debt` 先成立，
    它也成立不了之前，这条一律拒绝。
    """
    formula = "净债务 = 有息负债合计 − 非经营性现金"
    d = _interest_bearing_debt(lk, p, prior)
    if not d.ok:
        # ⚠ **把上游的原因带上来。** 只写「见那一条的说明」的话，
        #   看的人得先自己在网格里找到「有息负债合计」那一行才知道缺什么，
        #   而这一格点开时的**唯一**信息就是这句话。
        return _refuse(f"上游先卡住了：{d.refused}", formula)
    return _refuse(
        "「非经营性现金」没有对应字段，拿货币资金顶替会静默改变口径", formula,
        **d.inputs,
    )


def _roic(lk: Lookup, p: str, prior: str) -> DerivedResult:
    """字典的定义：`NOPAT / (有息负债 + 股东权益)`（交叉验证项）。

    NOPAT = 调整后 EBIT × (1 − 正常化税率)，其中税率取自周期正常化的
    运行结果——**这一条依赖 `normalization_run`，本模块拿不到**，
    所以先拒绝，理由写清楚。
    """
    formula = "投入资本回报率 = NOPAT ÷ (有息负债 + 股东权益)"
    return _refuse(
        "NOPAT 要「调整后 EBIT × (1 − 正常化税率)」，其中正常化税率来自周期"
        "正常化的运行结果，本层拿不到；且有息负债本身也还缺一个字段",
        formula,
    )


def _per_ton(lk: Lookup, p: str, prior: str, *, name: str, num_of: str,
             ) -> DerivedResult:
    """吨钢口径：分子 ÷ 钢材销量（吨）。

    ⚠ **分子与销量必须是同一口径。** 现在的销量只覆盖宝钢的
    「产销量情况分析表·合计」行（不含宝日汽车板的冷轧碳钢板卷），
    而毛利/EBITDA 是**全公司合并口径**——两者范围不一致。
    所以这一条算出来的值要标成「披露范围内」，不能当成全公司吨钢毛利。
    口径问题已打包问会计（`待会计确认.md` 问题 13）。
    """
    formula = f"{name} = {num_of} ÷ 钢材销量"
    if num_of == "毛利":
        num = _gross_profit(lk, p, prior)
    else:
        num = _ebitda(lk, p, prior)
    if not num.ok:
        return num
    vol = _get(lk, "steel_sales_volume", p)
    inputs = {**num.inputs, "钢材销量（吨）": str(vol)}
    if vol is None:
        return _refuse("缺 钢材销量，不插补", formula, **inputs)
    if vol == 0:
        return _refuse("钢材销量为 0，比率无定义", formula, **inputs)
    # ⚠ **量纲要先对齐。** 金额在库里以**百万元**存，销量以**吨**存，
    #   直接相除得到的是「百万元/吨」——写法上完全正常，结果是真值的
    #   百万分之一。实测宝钢 2024 会算出 `0.000799 元/吨`，而正确值是
    #   **799 元/吨**。乘 100 万才是「元/吨」。
    #   这类错不会报错，只会让一个吨钢指标看起来「小得离谱」。
    MILLION = Decimal("1000000")
    return DerivedResult(
        value=(num.value * MILLION / vol).quantize(PLACES),
        formula=formula + "（金额由百万元折算为元）", inputs=inputs,
        unit="元/吨", sources=num.sources + (SourceRef("steel_sales_volume", p),),
    )


def _steel_gross_profit_per_ton(lk: Lookup, p: str, prior: str) -> DerivedResult:
    return _per_ton(lk, p, prior, name="吨钢毛利", num_of="毛利")


def _steel_ebitda_per_ton(lk: Lookup, p: str, prior: str) -> DerivedResult:
    return _per_ton(lk, p, prior, name="吨钢 EBITDA", num_of="EBITDA")


def _steel_price_avg(lk: Lookup, p: str, _: str) -> DerivedResult:
    """字典的定义：`钢材销售收入 / 钢材销量`。

    ⚠ **「钢材销售收入」不是本系统采集的字段**（营业收入是全公司口径，
    含非钢业务）。拿营业收入顶替会把售价算高，且**看不出来**。
    """
    return _refuse(
        "「钢材销售收入」没有对应字段；拿全公司营业收入顶替会把平均售价算高",
        "钢材平均售价 = 钢材销售收入 ÷ 钢材销量",
    )


def _capacity_utilization(lk: Lookup, p: str, _: str) -> DerivedResult:
    return _refuse(
        "有效产能在本批 16 份年报里**未披露**（「有效产能」四个字一次都没出现），"
        "按 A-5 口径算不出来。年报直接披露的利用率另见 reported_capacity_utilization",
        "产能利用率 = 产量 ÷ 有效产能",
    )


def _not_applicable(name: str, why: str):
    def f(lk: Lookup, p: str, prior: str) -> DerivedResult:
        return _refuse(why, f"{name} = （本批样本不适用）")
    return f


#: 全部派生规则。**顺序即页面上的显示顺序**（继承字段字典的 display_order）。
SPECS: tuple[Derivation, ...] = (
    Derivation("gross_profit", "毛利", "百万元",
               ("revenue", "operating_cost"), False, _gross_profit),
    Derivation("ebit", "息税前利润", "百万元",
               ("profit_before_tax", "interest_expense", "interest_income"),
               False, _ebit),
    Derivation("ebitda", "息税折旧摊销前利润", "百万元",
               ("profit_before_tax", "interest_expense", "interest_income",
                "depreciation_amortization", "amortization_intangible",
                "right_of_use_asset_depreciation"), False, _ebitda),
    Derivation("gross_margin", "毛利率", "%", ("gross_profit", "revenue"),
               False, _gross_margin),
    Derivation("net_margin", "净利率", "%", ("net_income", "revenue"),
               False, _net_margin, formula_from_us=True),
    Derivation("ebit_margin", "EBIT 利润率", "%", ("ebit", "revenue"),
               False, _ebit_margin),
    Derivation("ebitda_margin", "EBITDA 利润率", "%", ("ebitda", "revenue"),
               False, _ebitda_margin),
    Derivation("cash_conversion", "现金转化率", "%", ("cfo", "net_income"),
               False, _cash_conversion),
    Derivation("roe", "净资产收益率", "%",
               ("net_income_parent", "equity_parent"), True, _roe),
    Derivation("nonrecurring_share", "非经常性损益占比", "%",
               ("non_recurring_gain_loss", "net_income"), False,
               _nonrecurring_share, formula_from_us=True),
    Derivation("interest_bearing_debt", "有息负债合计", "百万元",
               ("short_term_borrowing", "long_term_borrowing", "bonds_payable",
                "lease_liability",
                "non_current_liabilities_due_within_one_year"), False,
               _interest_bearing_debt),
    Derivation("operating_working_capital", "经营性营运资本", "百万元",
               ("accounts_receivable", "notes_receivable", "inventory",
                "accounts_payable", "notes_payable", "contract_liabilities"),
               False, _operating_working_capital),
    Derivation("net_debt", "净债务", "百万元", ("interest_bearing_debt",), False,
               _net_debt),
    Derivation("roic", "投入资本回报率", "%", (), False, _roic),
    Derivation("steel_price_avg", "钢材平均售价", "元/吨",
               ("steel_sales_volume",), False, _steel_price_avg),
    Derivation("steel_gross_profit_per_ton", "吨钢毛利", "元/吨",
               ("revenue", "operating_cost", "steel_sales_volume"), False,
               _steel_gross_profit_per_ton),
    Derivation("steel_ebitda_per_ton", "吨钢 EBITDA", "元/吨",
               ("ebitda", "steel_sales_volume"), False, _steel_ebitda_per_ton),
    Derivation("capacity_utilization", "产能利用率", "%",
               ("steel_output",), False, _capacity_utilization),
    Derivation("steel_spread", "吨钢原料差价", "元/吨", (), False,
               lambda lk, p, pr: _refuse(
                   "字段字典没有给算式；且它明确要求与「吨钢毛利」区分开"
                   "（差的是人工、制造费用与折旧），不能自己定",
                   "吨钢原料差价 = （未定义）")),
    Derivation("coal_price_avg", "吨煤平均售价", "元/吨", (), False,
               _not_applicable("吨煤平均售价", "本批三家都是钢铁企业，不产煤")),
    Derivation("ton_coal_gross_margin", "吨煤毛利", "元/吨", (), False,
               _not_applicable("吨煤毛利", "本批三家都是钢铁企业，不产煤")),
)

SPECS_BY_KEY: dict[str, Derivation] = {s.metric_key: s for s in SPECS}


def prior_of(period: str) -> str:
    """上一个年度。非四位年度原样返回（`roe` 会因为拿不到上期而拒绝）。"""
    if len(period) == 4 and period.isdigit():
        return str(int(period) - 1)
    return period


def compute_for(
    metric_key: str, period: str, lookup: Lookup
) -> DerivedResult | None:
    """算一个派生格。不是派生字段返回 None（调用方据此跳过）。"""
    spec = SPECS_BY_KEY.get(metric_key)
    if spec is None:
        return None
    return spec.compute(lookup, period, prior_of(period))
