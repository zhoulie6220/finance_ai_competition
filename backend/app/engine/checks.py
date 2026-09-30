"""三表勾稽校验：从数字反推解析错在哪。

背景
----
年报解析一定会出错：跨页表、合并单元格、「其中：」层级行、负数括号、单位只在
表头出现一次。这些错误**大部分不会让程序报错**——它们只是让某个数字不对，然后
一路流进比率、正常化和估值，最后变成一个看着合理但错的结论。

勾稽关系是唯一能自动抓住它们的办法：报表内部有若干恒等式，只要所有输入都解析
正确就必须成立。不成立说明**至少有一个数字解析错了**，而且能指出是哪一年、
哪张表、差多少。

> 勾稽不平**优先怀疑解析错误，而不是公司造假**。这是本项目反复强调的立场。

四条规则
--------
bs_equation         资产 = 负债 + 所有者权益
cash_rollforward    期初现金 + 现金净增加 = 期末现金
cf_components       现金净增加 = 经营 + 投资 + 筹资活动净额（+ 汇率变动影响）
gross_profit_check  毛利 = 营业收入 − 营业成本

用哪两个指标键、以及为什么，见每张规则函数上的注释。其中最要紧的一条：
**毛利必须用 `revenue`（营业收入）而不是 `total_revenue`（营业总收入）**——
两者在字典里是两行，2024 年恰好相等，所以只看 2024 年发现不了错配。

设计约束
--------
* **纯函数**：无 IO、无 datetime.now()、无 random、无全局状态。同输入必同输出。
* **Decimal**：金额一律用 Decimal，禁止 float。结果以字符串出库。
* **拒绝优于猜测**：缺数据返回 skipped_missing_data，**不等于通过**。宁可显示
  「未找到」，绝不猜。
* **可复算**：每个结果带 formula 与参与的 fact_id 列表，供证据链回溯。

输入由调用方（repository / skill 层）从库里读好再传进来——引擎自己不碰 sqlite。

已知的字典缺口（校验照出来的真实问题）
--------------------------------------
`cf_components` 在宝钢 2017–2024 全部不平，残差 −22 到 −292 百万元。原因是年报
现金流量表里有一行「**四、汇率变动对现金及现金等价物的影响**」位于三项活动净额
与「五、现金及现金等价物净增加额」之间，而字段字典里**没有对应的 metric_key**。
（原文见 2024 年报现金流量表页。）这不是解析错误，是字典缺字段——补齐后该规则
才能通过。在那之前它报 failed，suggestion 里点名这一行。

`gross_profit_check` 在本批数据上**永远 skipped_missing_data**：`gross_profit` 是
is_derived=1 的派生指标，743 条事实里一条都没有，没有可对照的披露值。规则本身是
对的，等有公司披露毛利就会生效。

状态码
------
passed                 恒等式成立
failed                 恒等式不成立，附带 lhs / rhs / diff
skipped_missing_data   缺输入，**不等于通过**
skipped_incomparable   输入被标记为不可比，按规则排除
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Mapping, Sequence

# ---------------------------------------------------------------- 状态码

PASSED = "passed"
FAILED = "failed"
SKIPPED_MISSING_DATA = "skipped_missing_data"
SKIPPED_INCOMPARABLE = "skipped_incomparable"

Status = Literal["passed", "failed", "skipped_missing_data", "skipped_incomparable"]

Severity = Literal["info", "warn", "error"]

# ---------------------------------------------------------------- 规则登记表

BS_EQUATION = "bs_equation"
CASH_ROLLFORWARD = "cash_rollforward"
CF_COMPONENTS = "cf_components"
GROSS_PROFIT_CHECK = "gross_profit_check"

# 规则文本集中在此，避免同一句话在通过路径与失败路径里写成两份、日后改漏一处。
RULE_LABELS: Mapping[str, str] = {
    BS_EQUATION: "资产 = 负债 + 所有者权益",
    CASH_ROLLFORWARD: "期初现金 + 现金净增加 = 期末现金",
    CF_COMPONENTS: "现金净增加 = 经营 + 投资 + 筹资活动净额（+ 汇率变动影响）",
    GROSS_PROFIT_CHECK: "毛利 = 营业收入 − 营业成本",
}

# 严重度：这条不平有多要紧。
#   error 报表本身不平，几可断定解析错了
#   warn  大概率是字典缺字段或口径差异，不是解析错误
#   info  参考性质
RULE_SEVERITY: Mapping[str, Severity] = {
    BS_EQUATION: "error",
    CASH_ROLLFORWARD: "error",
    CF_COMPONENTS: "warn",
    GROSS_PROFIT_CHECK: "info",
}

# ---------------------------------------------------------------- 指标键

# 每个恒等式用到的指标键。写成常量而不是散在函数里，是因为字典改键名时要
# 一处不漏地改——散着写的话漏掉一处不会报错，只会让那条规则永远 skipped。
M_TOTAL_ASSETS = "total_assets"
M_TOTAL_LIABILITIES = "total_liabilities"
M_TOTAL_EQUITY = "total_equity"
M_EQUITY_PARENT = "equity_parent"
M_MINORITY_INTEREST = "minority_interest"

M_CASH_BEGIN = "cash_begin"
M_CASH_END = "cash_end"
M_CASH_NET_INCREASE = "cash_net_increase"
M_CFO = "cfo"
M_CFI = "cfi"
M_CFF = "cff"

M_GROSS_PROFIT = "gross_profit"
M_REVENUE = "revenue"           # ⚠ 营业收入，不是营业总收入
M_OPERATING_COST = "operating_cost"

# 仅供公式文本里点明「用的是哪个」，**不参与取数**。
TOTAL_REVENUE_NOTE = "total_revenue（营业总收入）"

# 计算结果保留的小数位。金额保留 6 位与 normalization.py 一致。
MONEY_PLACES = Decimal("0.000001")

# 比对失败时 relative 容差缺省值。与 rule_config 的 check.* 初值保持一致，
# 由调用方从库里读出后经 ChecksConfig 传入。
DEFAULT_TOLERANCE = Decimal("0.005")


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class Fact:
    """一笔进入校验的财务事实。只带校验需要的东西。

    `comparable` 与 `incomparable_reason` 来自财务事实表——不可比的年份不参与
    校验，但整行保留（敏感性分析还要用）。
    """

    fact_id: str
    metric_key: str
    value: Decimal
    comparable: bool = True
    incomparable_reason: str | None = None


@dataclass(frozen=True)
class PeriodInput:
    """某个项目、某个报告期、某个口径下的一组事实。"""

    period: str
    scope: str
    facts: tuple[Fact, ...]

    def get(self, metric_key: str) -> Fact | None:
        """按指标键取事实。同一键重复出现时返回第一条——调用方应当已经去重。"""
        for f in self.facts:
            if f.metric_key == metric_key:
                return f
        return None

    def value(self, metric_key: str) -> Decimal | None:
        fact = self.get(metric_key)
        return None if fact is None else fact.value


@dataclass(frozen=True)
class ChecksConfig:
    """容差口径。初值与 rule_config 的 check.* 保持一致，可从库里读出后传入。

    容差都是**相对**的：允许偏差 = 比对基准 × tolerance。用相对值是因为资产
    几千亿、现金几百亿，一个绝对值容差不可能同时适配两者。
    """

    balance_tolerance: Decimal = DEFAULT_TOLERANCE
    cash_rollforward_tolerance: Decimal = DEFAULT_TOLERANCE
    cf_components_tolerance: Decimal = DEFAULT_TOLERANCE
    gross_profit_tolerance: Decimal = DEFAULT_TOLERANCE

    def __post_init__(self) -> None:
        for name in (
            "balance_tolerance",
            "cash_rollforward_tolerance",
            "cf_components_tolerance",
            "gross_profit_tolerance",
        ):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} 必须为正，收到 {value!r}")
            if value >= 1:
                raise ValueError(
                    f"{name} 是相对比例，取 {value!r} 等于允许 100% 以上的偏差，"
                    f"那样什么都能通过"
                )


@dataclass(frozen=True)
class CheckOutcome:
    """一条规则在一个期间上的结论。对应 fact_check_result 表的一行。"""

    rule_key: str
    period: str
    scope: str
    status: Status
    severity: Severity
    lhs: Decimal | None
    rhs: Decimal | None
    diff: Decimal | None
    tolerance: Decimal | None
    formula: str
    inputs: tuple[str, ...]
    message: str
    suggestion: str | None = None

    @property
    def ok(self) -> bool:
        """恒等式是否成立。**skipped 不是 ok**——没查不等于查过了。"""
        return self.status == PASSED

    @property
    def evaluable(self) -> bool:
        """这条规则在这个期间上是否**真的被评估过**。用于覆盖率统计。

        覆盖率必须按这个算，不能按「跑了多少条规则」算——缺数据的规则跑了也
        没有结论，把它算进分母会让覆盖率虚高。
        """
        return self.status in (PASSED, FAILED)


@dataclass(frozen=True)
class ChecksReport:
    """一个项目的全部期间校验结果。"""

    project_id: str
    scope: str
    periods: tuple[PeriodInput, ...]
    outcomes: tuple[CheckOutcome, ...]
    method_version: str = "checks:v1"

    @property
    def evaluable_count(self) -> int:
        return sum(1 for o in self.outcomes if o.evaluable)

    @property
    def total_count(self) -> int:
        return len(self.outcomes)

    @property
    def failures(self) -> tuple[CheckOutcome, ...]:
        """所有不成立的结论。"""
        return tuple(o for o in self.outcomes if o.status == FAILED)

    @property
    def hard_failures(self) -> tuple[CheckOutcome, ...]:
        """严重度为 error 的不平——**报表本身就不平**，几可断定解析错了。"""
        return tuple(o for o in self.failures if o.severity == "error")

    @property
    def soft_failures(self) -> tuple[CheckOutcome, ...]:
        """其余不平——大概率是字典缺字段或口径差异，不是解析错误。

        这一类的典型是 `cf_components` 的汇率变动残差：恒等式确实不成立，
        但原因是我们没采集那一行，不是采错了。两者都要报出来，但不能混为一谈——
        混在一起会让人把「字典该补一个字段」读成「报表有问题」。
        """
        return tuple(o for o in self.failures if o.severity != "error")

    @property
    def ok(self) -> bool:
        """全部**可评估**的规则都成立。

        ⚠ 缺数据不算不成立——但缺数据也**不算通过**。页面必须同时展示覆盖率，
        否则「44 项里 21 项可评估、全过」会被读成「44 项全过」。
        """
        return not self.failures

    @property
    def sheet_ok(self) -> bool:
        """报表本身是平的（没有 error 级不平）。字典缺口不影响它。"""
        return not self.hard_failures

    def coverage_line(self) -> str:
        """给人看的一句话覆盖率说明。

        **必须同时说出可评估项数与不平项数**，并且把两类不平分开说：
        报表不平（error）与字典缺字段（warn）的含义完全不同，混着报会让人
        把后者读成「公司有问题」。
        """
        if not self.failures:
            tail = "可评估项全部通过"
        else:
            bits = []
            if self.hard_failures:
                bits.append(f"{len(self.hard_failures)} 项不平")
            if self.soft_failures:
                bits.append(
                    f"{len(self.soft_failures)} 项存疑（多为字典缺字段，非解析错误）"
                )
            tail = "可评估项有 " + "、".join(bits)
        return (
            f"{self.total_count} 项期间校验中 {self.evaluable_count} 项可评估"
            f"（其余缺数据或不可比），{tail}"
        )


# ---------------------------------------------------------------- 工具函数


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY_PLACES)


def _scale(*values: Decimal | None) -> Decimal:
    """比对基准：参与比对各项里绝对值最大的那个。

    为什么不用固定的某一项做分母：期末现金可能接近零（某一年大额还款之后），
    拿它当分母会让相对误差爆掉，一个几百万的正常差异被算成几万倍。取最大绝对值
    就不会有这个问题。
    """
    present = [abs(v) for v in values if v is not None]
    return max(present) if present else Decimal(0)


def _compare(
    lhs: Decimal,
    rhs: Decimal,
    *,
    scale: Decimal,
    tolerance: Decimal,
) -> tuple[Decimal, Decimal, bool]:
    """返回 (差额, 允许偏差, 是否成立)。差额 = lhs − rhs。"""
    diff = lhs - rhs
    allowed = scale * tolerance
    return _quantize(diff), _quantize(allowed), abs(diff) <= allowed


def _missing(
    rule_key: str,
    p: PeriodInput,
    needed: Sequence[str],
    *,
    formula: str,
    extra: str = "",
) -> CheckOutcome:
    """构造「缺数据」结论。**刻意不判通过**——没查到不等于对得上。"""
    names = "、".join(needed)
    return CheckOutcome(
        rule_key=rule_key,
        period=p.period,
        scope=p.scope,
        status=SKIPPED_MISSING_DATA,
        severity=RULE_SEVERITY[rule_key],
        lhs=None,
        rhs=None,
        diff=None,
        tolerance=None,
        formula=formula,
        inputs=(),
        message=f"{p.period} 年：缺 {names}，无法校验。缺数据**不等于通过**。{extra}",
        suggestion=f"在年报中定位并录入 {names} 后重跑；核对字段字典的别名与排除词是否漏配。",
    )


def _incomparable(
    rule_key: str,
    p: PeriodInput,
    reasons: Sequence[str],
    *,
    formula: str,
) -> CheckOutcome:
    """构造「不可比」结论。不可比不进分母，但整行保留、理由必须写明。"""
    return CheckOutcome(
        rule_key=rule_key,
        period=p.period,
        scope=p.scope,
        status=SKIPPED_INCOMPARABLE,
        severity=RULE_SEVERITY[rule_key],
        lhs=None,
        rhs=None,
        diff=None,
        tolerance=None,
        formula=formula,
        inputs=(),
        message=f"{p.period} 年：{'；'.join(reasons)}，按规则不参与校验。",
        suggestion="不可比年度由会计确认后保留原样，供敏感性分析使用。",
    )


def _gate(
    rule_key: str,
    p: PeriodInput,
    needed: Sequence[str],
    *,
    formula: str,
) -> CheckOutcome | None:
    """取数前的两道闸：不可比、缺数据。都通过则返回 None。

    两类闸分开处理，因为它们的含义完全不同：不可比是**已知且经过确认**的
    口径问题，缺数据是**还不知道**。混在一起报会让人以为后者也已确认。
    """
    facts = []
    for key in needed:
        fact = p.get(key)
        if fact is None:
            return _missing(rule_key, p, needed, formula=formula)
        facts.append(fact)

    incomparable = [
        f"{f.metric_key}（{f.incomparable_reason or '未注明原因'}）"
        for f in facts
        if not f.comparable
    ]
    if incomparable:
        return _incomparable(rule_key, p, incomparable, formula=formula)
    return None


# ---------------------------------------------------------------- 规则一：资产 = 负债 + 所有者权益


BS_FORMULA = "资产总计 = 负债合计 + 所有者权益合计"


def check_bs_equation(p: PeriodInput, cfg: ChecksConfig) -> CheckOutcome:
    """资产 = 负债 + 所有者权益。

    所有者权益的取法有讲究：

      1. 优先用 **所有者权益合计**（`total_equity`）——年报上直接披露的一行。
      2. 没有合计行时，用 **归属于母公司股东权益 + 少数股东权益**（`equity_parent`
         + `minority_interest`）。这是把两个**各自独立披露**的组成部分相加，
         不是反推。

    ⚠ **绝不允许用「资产 − 负债」反推所有者权益**。那样这个恒等式会变成同义
    反复，永远成立、永远抓不住任何解析错误——正是本项目最怕的那种「不报错但
    静默失效」。下面 `_assert_disjoint` 就是这条的机器化保障。

    覆盖情况（宝钢 2014–2024）：合计行只有 2014–2017，加上归母+少数股东这条
    路径能到 2018；2019 年起年报的权益行未被解析出来，落 skipped_missing_data。
    """
    assets = p.get(M_TOTAL_ASSETS)
    liabilities = p.get(M_TOTAL_LIABILITIES)
    if assets is None or liabilities is None:
        return _missing(
            BS_EQUATION,
            p,
            [M_TOTAL_ASSETS, M_TOTAL_LIABILITIES],
            formula=BS_FORMULA,
        )

    equity_facts, equity_desc = _equity_parts(p)
    if equity_facts is None:
        return _missing(
            BS_EQUATION,
            p,
            [M_TOTAL_EQUITY, f"{M_EQUITY_PARENT} + {M_MINORITY_INTEREST}"],
            formula=BS_FORMULA,
            extra="所有者权益合计与「归母 + 少数股东权益」两条路径都取不到。",
        )

    lhs_facts = [assets]
    rhs_facts = [liabilities, *equity_facts]

    incomparable = [
        f"{f.metric_key}（{f.incomparable_reason or '未注明原因'}）"
        for f in (*lhs_facts, *rhs_facts)
        if not f.comparable
    ]
    if incomparable:
        return _incomparable(BS_EQUATION, p, incomparable, formula=BS_FORMULA)

    # 恒等式两边的输入**必须来自不同的行**。用了同一笔事实（或拿资产去反推权益）
    # 等式就成了同义反复，永远成立——那是逻辑写错，不是数据问题，所以要抛错。
    _assert_disjoint(lhs_facts, rhs_facts, BS_EQUATION)

    lhs = assets.value
    rhs = sum((f.value for f in rhs_facts), Decimal(0))
    diff, allowed, passed = _compare(
        lhs, rhs, scale=_scale(lhs, rhs), tolerance=cfg.balance_tolerance
    )

    formula = f"{BS_FORMULA}；其中所有者权益取「{equity_desc}」"
    inputs = tuple(f.fact_id for f in (*lhs_facts, *rhs_facts))

    if passed:
        return CheckOutcome(
            rule_key=BS_EQUATION,
            period=p.period,
            scope=p.scope,
            status=PASSED,
            severity=RULE_SEVERITY[BS_EQUATION],
            lhs=_quantize(lhs),
            rhs=_quantize(rhs),
            diff=diff,
            tolerance=allowed,
            formula=formula,
            inputs=inputs,
            message=f"{p.period} 年资产负债表平衡：{lhs} = {rhs}（差 {diff}，容差 {allowed}）。",
        )

    return CheckOutcome(
        rule_key=BS_EQUATION,
        period=p.period,
        scope=p.scope,
        status=FAILED,
        severity=RULE_SEVERITY[BS_EQUATION],
        lhs=_quantize(lhs),
        rhs=_quantize(rhs),
        diff=diff,
        tolerance=allowed,
        formula=formula,
        inputs=inputs,
        message=(
            f"★ {p.period} 年资产负债表不平：资产 {lhs}，负债+权益 {rhs}，"
            f"差额 {diff} 百万元（超过容差 {allowed}）。"
        ),
        suggestion=(
            f"优先怀疑解析错误而不是造假。逐一核对这几行的原文与单位："
            f"{'、'.join(f.metric_key for f in (*lhs_facts, *rhs_facts))}；"
            f"常见原因是「其中：」层级行被当成合计行、合并/母公司口径混用、"
            f"或少数股东权益被漏掉。"
        ),
    )


def _equity_parts(p: PeriodInput) -> tuple[list[Fact] | None, str]:
    """所有者权益的构成。返回 (事实列表, 中文说明)；取不到时 (None, "")。"""
    total = p.get(M_TOTAL_EQUITY)
    if total is not None:
        return [total], "所有者权益合计"

    parent = p.get(M_EQUITY_PARENT)
    minority = p.get(M_MINORITY_INTEREST)
    if parent is not None and minority is not None:
        return [parent, minority], "归属于母公司股东权益 + 少数股东权益"
    return None, ""


def _assert_disjoint(
    lhs_facts: Sequence[Fact], rhs_facts: Sequence[Fact], rule_key: str
) -> None:
    """恒等式两边不得共用同一笔事实。

    共用意味着等式被写成了同义反复（例如拿「资产 − 负债」当权益），它会永远
    成立、永远抓不住任何东西，而且**不会报任何错**。这属于程序员错误，抛
    ValueError 而不是返回失败状态。
    """
    overlap = {f.fact_id for f in lhs_facts} & {f.fact_id for f in rhs_facts}
    if overlap:
        raise ValueError(
            f"{rule_key} 的等式两边共用了同一笔事实 {sorted(overlap)}——"
            f"恒等式会退化成同义反复，永远成立、永远抓不住解析错误"
        )


# ---------------------------------------------------------------- 规则二：期初现金 + 现金净增加 = 期末现金


CASH_ROLLFORWARD_FORMULA = "期初现金及现金等价物 + 现金及现金等价物净增加额 = 期末现金及现金等价物"


def check_cash_rollforward(p: PeriodInput, cfg: ChecksConfig) -> CheckOutcome:
    """现金流量表两端对得上：期初 + 本期净增加 = 期末。

    这条在宝钢 2017–2024 全部成立且**精确到小数点后 6 位**，是覆盖率最好、
    最可信的一条——演示时优先用它。

    2014–2016 缺期初期末两行（那几年的年报只解析出了净增加额），落
    skipped_missing_data。
    """
    gate = _gate(
        CASH_ROLLFORWARD,
        p,
        [M_CASH_BEGIN, M_CASH_NET_INCREASE, M_CASH_END],
        formula=CASH_ROLLFORWARD_FORMULA,
    )
    if gate is not None:
        return gate

    begin = p.value(M_CASH_BEGIN)
    net = p.value(M_CASH_NET_INCREASE)
    end = p.value(M_CASH_END)
    assert begin is not None and net is not None and end is not None

    lhs, rhs = end, begin + net
    diff, allowed, passed = _compare(
        lhs, rhs, scale=_scale(lhs, rhs), tolerance=cfg.cash_rollforward_tolerance
    )
    facts = (M_CASH_BEGIN, M_CASH_NET_INCREASE, M_CASH_END)

    if passed:
        return CheckOutcome(
            rule_key=CASH_ROLLFORWARD,
            period=p.period,
            scope=p.scope,
            status=PASSED,
            severity=RULE_SEVERITY[CASH_ROLLFORWARD],
            lhs=_quantize(lhs),
            rhs=_quantize(rhs),
            diff=diff,
            tolerance=allowed,
            formula=CASH_ROLLFORWARD_FORMULA,
            inputs=tuple(f.fact_id for f in (p.get(k) for k in facts) if f),
            message=f"{p.period} 年现金滚动成立：{lhs} = {rhs}（差 {diff}）。",
        )

    return CheckOutcome(
        rule_key=CASH_ROLLFORWARD,
        period=p.period,
        scope=p.scope,
        status=FAILED,
        severity=RULE_SEVERITY[CASH_ROLLFORWARD],
        lhs=_quantize(lhs),
        rhs=_quantize(rhs),
        diff=diff,
        tolerance=allowed,
        formula=CASH_ROLLFORWARD_FORMULA,
        inputs=tuple(f.fact_id for f in (p.get(k) for k in facts) if f),
        message=(
            f"★ {p.period} 年现金滚动不平：期末现金 {lhs}，"
            f"期初+净增加 {rhs}，差额 {diff} 百万元（超过容差 {allowed}）。"
        ),
        suggestion=(
            "核对现金流量表首行与末行。常见原因是把「其中：」子项当成了合计、"
            "期初数取成了母公司口径，或单位换算时少了三个数量级。"
        ),
    )


# ---------------------------------------------------------------- 规则三：现金净增加 = 三项活动净额


CF_COMPONENTS_FORMULA = "现金及现金等价物净增加额 = 经营活动 + 投资活动 + 筹资活动净额（+ 汇率变动影响）"

FX_SUGGESTION = (
    "本差异**不是解析错误**：年报现金流量表在「三项活动净额」与「现金及现金等价物"
    "净增加额」之间还有一行「四、汇率变动对现金及现金等价物的影响」，"
    "而字段字典里没有对应的 metric_key。残差即该行金额。"
    "补齐字典（新增 fx_effect_on_cash 之类）并录入后本规则即可通过。"
)


def check_cf_components(p: PeriodInput, cfg: ChecksConfig) -> CheckOutcome:
    """现金净增加 = 经营 + 投资 + 筹资活动净额。

    ⚠ 完整的恒等式还要加「汇率变动对现金及现金等价物的影响」。字典里没有这一项，
    所以本规则在宝钢 2017–2024 全部报不平，残差 −22 到 −292 百万元，逐年不同。

    刻意**照实报 failed 而不是放宽容差绕过**：把容差调大到能吞下残差，等于把这条
    规则废掉——残差本来就该等于汇率影响，调容差会让真正的解析错误也一起溜过去。
    正确做法是补字典字段，suggestion 里已经点名了。
    """
    gate = _gate(
        CF_COMPONENTS,
        p,
        [M_CASH_NET_INCREASE, M_CFO, M_CFI, M_CFF],
        formula=CF_COMPONENTS_FORMULA,
    )
    if gate is not None:
        return gate

    net = p.value(M_CASH_NET_INCREASE)
    cfo = p.value(M_CFO)
    cfi = p.value(M_CFI)
    cff = p.value(M_CFF)
    assert net is not None and cfo is not None and cfi is not None and cff is not None

    lhs = net
    rhs = cfo + cfi + cff
    diff, allowed, passed = _compare(
        lhs, rhs, scale=_scale(lhs, rhs), tolerance=cfg.cf_components_tolerance
    )
    keys = (M_CASH_NET_INCREASE, M_CFO, M_CFI, M_CFF)
    inputs = tuple(f.fact_id for f in (p.get(k) for k in keys) if f)

    if passed:
        return CheckOutcome(
            rule_key=CF_COMPONENTS,
            period=p.period,
            scope=p.scope,
            status=PASSED,
            severity=RULE_SEVERITY[CF_COMPONENTS],
            lhs=_quantize(lhs),
            rhs=_quantize(rhs),
            diff=diff,
            tolerance=allowed,
            formula=CF_COMPONENTS_FORMULA,
            inputs=inputs,
            message=f"{p.period} 年现金流量表三项活动合计与净增加额一致（差 {diff}）。",
        )

    return CheckOutcome(
        rule_key=CF_COMPONENTS,
        period=p.period,
        scope=p.scope,
        status=FAILED,
        severity=RULE_SEVERITY[CF_COMPONENTS],
        lhs=_quantize(lhs),
        rhs=_quantize(rhs),
        diff=diff,
        tolerance=allowed,
        formula=CF_COMPONENTS_FORMULA,
        inputs=inputs,
        message=(
            f"{p.period} 年：现金净增加额 {lhs}，三项活动合计 {rhs}，"
            f"差额 {diff} 百万元。该差额与「汇率变动对现金及现金等价物的影响」"
            f"一行相符，而字典中无此字段。"
        ),
        suggestion=FX_SUGGESTION,
    )


# ---------------------------------------------------------------- 规则四：毛利 = 营业收入 − 营业成本


GROSS_PROFIT_FORMULA = "毛利 = 营业收入 − 营业成本"


def check_gross_profit(p: PeriodInput, cfg: ChecksConfig) -> CheckOutcome:
    """毛利 = 营业收入 − 营业成本。

    ⚠ **必须是 `revenue`（营业收入）而不是 `total_revenue`（营业总收入）。**
    字典里这是两行不同的东西：营业总收入 = 营业收入 + 其他业务收入等。
    混用会让毛利率差零点几个百分点，**而且在 2024 年发现不了**——那一年宝钢
    两个数字恰好相等（都是 322,116 百万元）。只有翻到 2015–2023 才看得出。

    本批数据上这条规则**永远返回 skipped_missing_data**：`gross_profit` 是
    is_derived=1 的派生指标，743 条事实里一条都没有，没有可对照的披露值。
    规则本身是对的，等有公司披露毛利就会生效——保留它是因为「毛利」是会计口径
    里明确要求的一条勾稽，删掉会让覆盖矩阵缺一块。
    """
    disclosed = p.get(M_GROSS_PROFIT)
    if disclosed is None:
        return _missing(
            GROSS_PROFIT_CHECK,
            p,
            [M_GROSS_PROFIT],
            formula=GROSS_PROFIT_FORMULA,
            extra=(
                "毛利是派生指标（is_derived=1），本批数据没有披露值。"
                "等有公司直接披露毛利时本规则才会评估。"
            ),
        )

    gate = _gate(
        GROSS_PROFIT_CHECK,
        p,
        [M_REVENUE, M_OPERATING_COST],
        formula=GROSS_PROFIT_FORMULA,
    )
    if gate is not None:
        return gate

    revenue_fact = p.get(M_REVENUE)
    cost_fact = p.get(M_OPERATING_COST)
    assert revenue_fact is not None and cost_fact is not None

    # 披露的毛利必须与「收入」「成本」是各自独立的三行，不能自引用
    _assert_disjoint([disclosed], [revenue_fact, cost_fact], GROSS_PROFIT_CHECK)

    lhs, rhs = disclosed.value, revenue_fact.value - cost_fact.value
    diff, allowed, passed = _compare(
        lhs, rhs, scale=_scale(lhs, rhs), tolerance=cfg.gross_profit_tolerance
    )
    keys = (M_GROSS_PROFIT, M_REVENUE, M_OPERATING_COST)
    inputs = tuple(f.fact_id for f in (p.get(k) for k in keys) if f)
    formula = f"{GROSS_PROFIT_FORMULA}（营业收入取 {M_REVENUE}，不是 {TOTAL_REVENUE_NOTE}）"

    if passed:
        return CheckOutcome(
            rule_key=GROSS_PROFIT_CHECK,
            period=p.period,
            scope=p.scope,
            status=PASSED,
            severity=RULE_SEVERITY[GROSS_PROFIT_CHECK],
            lhs=_quantize(lhs),
            rhs=_quantize(rhs),
            diff=diff,
            tolerance=allowed,
            formula=formula,
            inputs=inputs,
            message=f"{p.period} 年毛利校验通过：{lhs} = {rhs}（差 {diff}）。",
        )

    return CheckOutcome(
        rule_key=GROSS_PROFIT_CHECK,
        period=p.period,
        scope=p.scope,
        status=FAILED,
        severity=RULE_SEVERITY[GROSS_PROFIT_CHECK],
        lhs=_quantize(lhs),
        rhs=_quantize(rhs),
        diff=diff,
        tolerance=allowed,
        formula=formula,
        inputs=inputs,
        message=(
            f"{p.period} 年毛利不平：披露 {lhs}，营业收入−营业成本 {rhs}，"
            f"差额 {diff} 百万元。"
        ),
        suggestion=(
            f"先确认营业成本没有误取「营业总成本」（含税金及附加与四项期间费用，"
            f"会让毛利率变成营业利润率）；再确认营业收入没有误取「营业总收入」。"
        ),
    )


# ---------------------------------------------------------------- 编排


ALL_RULES = (BS_EQUATION, CASH_ROLLFORWARD, CF_COMPONENTS, GROSS_PROFIT_CHECK)


def run_checks(
    periods: Sequence[PeriodInput],
    *,
    project_id: str = "",
    scope: str = "",
    cfg: ChecksConfig | None = None,
) -> ChecksReport:
    """对每个期间跑全部规则。

    每条规则独立求值：一条缺数据不影响其他条。这样覆盖矩阵才能如实反映
    「哪些年份、哪些规则真的查过」。
    """
    settings = cfg or ChecksConfig()
    outcomes: list[CheckOutcome] = []
    for p in periods:
        outcomes.append(check_bs_equation(p, settings))
        outcomes.append(check_cash_rollforward(p, settings))
        outcomes.append(check_cf_components(p, settings))
        outcomes.append(check_gross_profit(p, settings))

    return ChecksReport(
        project_id=project_id,
        scope=scope or (periods[0].scope if periods else ""),
        periods=tuple(periods),
        outcomes=tuple(outcomes),
    )
