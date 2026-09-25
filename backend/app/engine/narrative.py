"""叙事—财务一致性：从管理层措辞里认出**可验证主张**，再拿财务事实去验。

纯函数、零 IO、零 LLM。「数字由程序计算，模型只负责理解和组织文字」这条边界在这里
同样成立：模型可以帮忙**找句子**，但「这句话兑现了没有」是比大小，程序说了算。

## 为什么先做规则法，而不是直接上模型

设计上主张抽取是「规则优先、LLM 兜底」（见 `skills/__init__.py`）。规则法的价值不在
准确率，在于它是**LLM 版本的对照组**：同一条主张，规则命中在哪、判成什么，
是可以逐句复算的。模型版本上线后，两者的分歧就是人工复核的入口——
没有这个基线，「模型判错了」和「规则太糙」分不出来。

## ⚠ 词表是拿真实语料试出来的，不是想出来的

在宝钢 2015–2024 十份年报上实测后改过两轮，两个反直觉的结论：

**1. 「产能释放」不能算利好。** 2024 年报原话是「市场有效需求不足、钢铁产能释放较快，
铁矿石价格高位运行，行业盈利空间受到挤压」——**产能释放在这里是利空**，说的是供给过剩。
按词面把它当利好主张，系统会在一份基调悲观的年报上判出「管理层看好产能扩张」。
同类的还有「需求」本身：「需求不足」和「需求旺盛」共用两个字。

**2. 结构化词多是工程名。** 「结构优化」在宝钢 2024 命中 6 句，其中 5 句是
「资金主要用于宝山基地无取向硅钢产品结构优化工程」——那是投资项目的名字，
不是经营成果的表述。所以词表除了关键词还要有**排除词**，
这和字段字典 `metric_definition.aliases` / `exclude_words` 是同一套思路。

结论：**只收措辞明确、方向无歧义的短语**，含糊的一律不当主张。
判定不出来就记 `missing`，不猜——这与引擎其余部分的「拒绝优于猜测」是同一条规则。

## 这里不算诊断指数

本模块只出**逐条观测**（支持 / 冲突 / 不可比 / 缺失）与计数，不出分数。
指数的公式、阈值、到估值情景的映射属会计口径，由 `docs/04-index-rules.md` 定，
在那份手册落地之前 `index.py` 不该存在——凭空猜一个公式，猜完必返工。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.engine.ratios import period_gap

#: 观测的四种状态。与项目设计的四态一致，也正好是前端那张对照表的四种配色。
SUPPORTED = "supported"
CONFLICTED = "conflicted"
INCOMPARABLE = "incomparable"
MISSING = "missing"

STATE_CN = {
    SUPPORTED: "支持",
    CONFLICTED: "冲突",
    INCOMPARABLE: "不可比",
    MISSING: "缺失",
}


# ------------------------------------------------------------------ 词表


@dataclass(frozen=True)
class MetricRef:
    """用来验证一条主张的指标。

    `key` 既可以是 `financial_fact.metric_key`（取数用），
    也可以是派生量 `gross_margin`——年报里没有「毛利率」这一行，
    它由「毛利 / 营业收入」算得，退回去要两个原始科目。
    """

    key: str
    label_cn: str
    unit: str


@dataclass(frozen=True)
class Theme:
    """一类可验证主张。"""

    key: str
    label_cn: str
    #: 命中即算候选主张。**每条都必须是方向无歧义的向好措辞**——
    #: 见模块开头，「产能释放」这类会随上下文翻转的词一律不收。
    keywords: tuple[str, ...]
    #: 含任一排除词的句子不算主张（多是工程名、募集资金用途之类的非经营表述）。
    exclude: tuple[str, ...]
    metric: MetricRef
    #: 主张成立时，指标应当朝哪边走。'up' 表示「说了变好，那就该真的变好」。
    expect: str
    #: 为什么用这个指标验证，写进证据面板给评审看。
    basis_cn: str


#: 全局否定词。含任一，则该句是**风险陈述**而不是向好主张。
#: 不设这一层的话，「回款未见改善」会被当成「回款改善」命中。
#:
#: ⚠ 两条**不能**收进来的，都是写第一版时踩的坑：
#:
#:   「未」  —— 它会命中「**未**来」。而前瞻段里「未来」遍地都是，
#:            收进来等于把整段展望静默丢光。
#:   「下降」—— 「成本下降」正是「降本增效」这类主张的正面措辞，收进来会
#:            把要验的主张本身过滤掉，表现为「这家公司从来不提降本」。
#:
#: 所以否定词一律用指向明确的二字词，不用单字。
_NEGATION = (
    "没有", "难以", "无法", "不足", "尚未", "未能", "未见", "未明显",
    "压力", "承压", "挑战", "风险", "疲软", "低迷", "下滑", "恶化",
)

_THEMES: tuple[Theme, ...] = (
    Theme(
        key="demand",
        label_cn="需求与产销",
        keywords=("需求旺盛", "需求向好", "产销两旺", "满产满销", "供不应求", "订单饱满"),
        exclude=("预计", "有望"),
        metric=MetricRef("revenue", "营业收入", "百万元"),
        expect="up",
        basis_cn="钢材是标准化产品，需求向好最终要落到营业收入上",
    ),
    Theme(
        key="capacity",
        label_cn="产能与效率",
        keywords=("满负荷生产", "达产", "产能利用率提升", "制造效率提升", "稳产高产"),
        # ⚠ 「产能释放」**刻意不收**：2024 年报里它是利空（供给过剩），
        #   收进来会让系统在悲观的年报上判出乐观主张。理由见模块开头。
        exclude=("工程", "项目", "在建", "预计", "计划"),
        metric=MetricRef("revenue", "营业收入", "百万元"),
        expect="up",
        basis_cn="产量释放而价格不变时，营业收入同步上升",
    ),
    Theme(
        key="cost",
        label_cn="降本增效",
        keywords=("降本增效", "成本削减", "成本下降", "工序成本", "降本潜力"),
        exclude=("压力", "挤压", "上升"),
        # 成本下来 → 毛利上去。所以验的是毛利率而不是成本额本身：
        # 成本额随产量一起涨是正常的，涨得比收入慢才叫降本。
        metric=MetricRef("gross_margin", "毛利率", "%"),
        expect="up",
        basis_cn="成本降得比收入快，毛利率才会上升；只看成本额会被产量变化带偏",
    ),
    Theme(
        key="mix",
        label_cn="产品结构升级",
        # ⚠ 刻意**不收**光秃秃的「结构优化」与「差异化」，两者都在实测里翻了车：
        #   「结构优化」命中的多是工程名（见下面的排除词），
        #   「差异化」则命中了「同期原材料呈现差异化，62%铁矿石普氏指数…」——
        #   那是一句行业行情描述，跟这家公司做了什么毫无关系。
        #   词表要的是「主语是公司」的表述，所以一律带上前缀把主语锁死。
        keywords=("产品结构优化", "优化产品结构", "结构升级", "品种钢", "高端产品"),
        # ⚠ 这几个排除词是实测加的：宝钢 2024 的「结构优化」6 句里有 5 句是
        #   「无取向硅钢产品结构优化工程」，说的是项目名，不是经营成果。
        #   「基地」「改造」「大修」是同类的漏网——「宝山基地条钢厂产品结构优化改造：
        #   为满足新能源汽车…」是一条**工程立项说明**，界面上却会显示成
        #   「管理层说产品结构要升级」，评审一看就知道系统没读懂。
        exclude=(
            "工程", "项目", "资金主要", "募集", "投资", "原材料",
            "基地", "改造", "大修", "在建",
        ),
        metric=MetricRef("gross_margin", "毛利率", "%"),
        expect="up",
        basis_cn="高端品种占比提升，直接体现为毛利率改善",
    ),
    Theme(
        key="cash",
        label_cn="回款与现金流",
        keywords=("两金压降", "回款改善", "现金流改善", "应收账款周转", "现款现货"),
        exclude=("预计", "计划"),
        metric=MetricRef("cfo", "经营活动现金流量净额", "百万元"),
        expect="up",
        basis_cn="回款是否真的改善，只有经营现金流能验",
    ),
)

THEMES: dict[str, Theme] = {t.key: t for t in _THEMES}


# ------------------------------------------------------------------ 主张


@dataclass(frozen=True)
class Utterance:
    """年报里的一句话，带出处。

    `forward=True` 表示它出自**前瞻段**（「公司关于公司未来发展的讨论与分析」
    这类），说的是下一年的事 —— 验证对象因此要往后挪一年。
    不区分的话，前瞻主张会拿去和本年数字比，方向多半对不上，
    于是全被judged成冲突，而它们其实只是「还没到时候」。
    """

    text: str
    page_no: int
    period: str
    forward: bool = False
    #: 出自哪一份年报。与 `page_no` 合起来才是完整出处——
    #: 只说「第 26 页」而公司有十份年报时，这个引用指不到任何地方。
    source_file: str = ""


@dataclass(frozen=True)
class Claim:
    """一条可验证主张。"""

    theme_key: str
    theme_label: str
    text: str
    page_no: int
    matched: str
    #: 这句话出自哪一份年报
    source_file: str
    #: 这句话出自哪一年的年报
    source_period: str
    #: 应该拿哪一年的财务事实去验
    verify_period: str
    forward: bool
    metric: MetricRef
    #: 主张成立时指标该朝哪边走（来自主题表）。**必须跟着主张一起走**，
    #: 不能验证时再去查主题表——那会让「同一条主张」在不同调用点判出不同结果。
    expect: str = "up"

    @property
    def source_text(self) -> str:
        """证据面板里显示的原句。**照抄，不做任何加工。**"""
        return self.text


@dataclass(frozen=True)
class Observation:
    """一条主张的验证结果。"""

    claim: Claim
    state: str
    reason: str
    formula: str = ""
    inputs: dict[str, str] = field(default_factory=dict)
    #: 验证时用到的实际数值，供界面显示（如「毛利率 5.45% → 4.20%」）
    actual: str = ""

    @property
    def state_cn(self) -> str:
        return STATE_CN.get(self.state, self.state)


def _next_year(period: str) -> str:
    """下一年。解析不了就原样返回——调用方会因此把前瞻主张当作当年验，
    那会在 `verify_period` 上体现出来，不会静默算错一个方向。"""
    try:
        return str(int(period) + 1)
    except (TypeError, ValueError):
        return period


def find_claims(utterances: list[Utterance]) -> list[Claim]:
    """从句子流里认出可验证主张。

    三类句子会被跳过，**每一类都是实测踩出来的**：

      1. 含否定/风险词的 —— 那是风险陈述，不是向好主张
      2. 含排除词的 —— 多是工程名、募集用途，不是经营成果的表述
      3. 同一 (主题, 年报, 页码) 的重复命中 —— 一句话里出现两次同主题词很常见

    第 3 条尤其要留着：主张是「这家公司在 2024 年报里说了它产能满负荷」，
    一句话命中两次仍是**一条**主张。按命中次数计数会让词频高的公司显得主张更多，
    而那只是它话多。
    """
    out: list[Claim] = []
    seen: set[tuple[str, str, int]] = set()

    for ut in utterances:
        text = ut.text.strip()
        if not text:
            continue
        if any(n in text for n in _NEGATION):
            continue
        for theme in _THEMES:
            if any(x in text for x in theme.exclude):
                continue
            hit = next((k for k in theme.keywords if k in text), None)
            if hit is None:
                continue
            dedup = (theme.key, ut.period, ut.page_no)
            if dedup in seen:
                continue
            seen.add(dedup)
            out.append(
                Claim(
                    theme_key=theme.key,
                    theme_label=theme.label_cn,
                    text=text,
                    page_no=ut.page_no,
                    matched=hit,
                    source_file=ut.source_file,
                    source_period=ut.period,
                    verify_period=_next_year(ut.period) if ut.forward else ut.period,
                    forward=ut.forward,
                    metric=theme.metric,
                    expect=theme.expect,
                )
            )
    return out


# ------------------------------------------------------------------ 验证


#: 变动小于这个相对幅度就算「没动」，不参与判定。
#:
#: ⚠ **没有这一层，噪声会把结论淹掉。** 实测宝钢 2019 年毛利率从 10.8791%
#: 走到 10.8350%，动了 0.04 个百分点，就被判成「管理层说降本增效，事实相悖」。
#: 那种量级在会计上什么都不说明，而它和真正的背离（2018 年 14.99% → 10.88%）
#: 在那张四态表里长得一模一样——评审一眼就会看出系统分不清噪声与信号。
#:
#: 这是**会计口径参数**，不是工程常数：多少算「动了」因行业而异。
#: 当前值属暂定，已登记进 `rule_config` 待会计同学确认（tier='hard'）。
DEFAULT_MIN_REL_CHANGE = Decimal("0.01")


def _direction(
    prev: Decimal | None,
    curr: Decimal | None,
    *,
    label: str,
    prev_period: str,
    curr_period: str,
    unit: str,
    min_rel_change: Decimal = DEFAULT_MIN_REL_CHANGE,
) -> tuple[str | None, str, str, dict[str, str]]:
    """比方向。返回 (up/down/flat/None, reason, actual, inputs)。

    ⚠ 这里**直接比大小**，不套增长率公式。原因：增长率在基期为负时会失去方向含义
    （−100 → +50 算出来是 −150%，但那是扭亏，是变好），而叙事验证要的恰恰是方向。
    比率（毛利率）本来就该用百分点差，也不适用增长率。

    判定用的是**相对变动**而非绝对变动，这样金额（百万元）与比率（%）可以共用
    同一个阈值——绝对变动在两者之间没有可比性（10 个百万和 10 个百分点）。
    """
    inputs = {f"{prev_period} 年": str(prev), f"{curr_period} 年": str(curr)}
    formula = f"{label}：{prev_period} 年 {prev} → {curr_period} 年 {curr} {unit}"

    if prev is None or curr is None:
        missing = prev_period if prev is None else curr_period
        return None, f"{missing} 年没有该指标的数据，无法验证", "", inputs

    gap = period_gap(prev_period, curr_period)
    if gap is None:
        return None, "期间无法解析为年份", "", inputs
    if gap != 1:
        return None, f"期间不连续：{prev_period} 与 {curr_period} 隔了 {gap} 年", "", inputs

    delta = curr - prev
    actual = f"{prev} → {curr}（{'+' if delta > 0 else ''}{delta} {unit}）"

    if prev == 0:
        # 基期为 0 时相对变动无定义，但方向本身仍然可判（0 → 正数就是上行）。
        if delta > 0:
            return "up", "", actual, inputs
        if delta < 0:
            return "down", "", actual, inputs
        return "flat", "", actual, inputs

    rel = abs(delta) / abs(prev)
    if rel < min_rel_change:
        # ⚠ 解释必须放在 `reason` 而不是 `actual` 里：`verify()` 在 reason 为空时
        #   会填一句兜底文案，而那句兜底把具体幅度和阈值全盖掉了。
        #   用户看到的会是「毛利率未变动」——**看不出它其实动了 0.41%**，
        #   也就无从判断这个阈值定得合不合理。
        return (
            "flat",
            f"变动 {rel:.2%}（{actual}）低于重要性阈值 {min_rel_change:.0%}，"
            "视为未变动，不足以判断主张是否兑现",
            actual,
            inputs,
        )

    return ("up" if delta > 0 else "down"), "", actual, inputs


def verify(
    claim: Claim,
    series: dict[str, Decimal | None],
    *,
    prev_period: str | None = None,
    min_rel_change: Decimal = DEFAULT_MIN_REL_CHANGE,
) -> Observation:
    """拿一个指标的逐年序列去验一条主张。

    `series` 是 {年份: 数值}，由调用方从 `v_fact_verified` 取好传进来——
    引擎不碰数据库。缺的年份**放进去并置 None**，不要省略键：
    「这一年没披露」和「我没查这一年」是两件事，前者要报缺失，后者是调用方的 bug。
    """
    period = claim.verify_period
    prev = prev_period or (
        str(int(period) - 1) if period.isdigit() else None
    )
    if prev is None:
        return Observation(claim, INCOMPARABLE, f"无法确定 {period} 年的上一年")

    direction, reason, actual, inputs = _direction(
        series.get(prev), series.get(period),
        label=claim.metric.label_cn,
        prev_period=prev, curr_period=period, unit=claim.metric.unit,
        min_rel_change=min_rel_change,
    )

    if direction is None:
        # 分不清「没数据」与「算不了」：两者对用户要做的事不同，
        # 所以按 `reason` 里那句具体的话来定状态，不压成同一个词。
        state = MISSING if "没有该指标的数据" in reason else INCOMPARABLE
        return Observation(claim, state, reason, inputs=inputs)

    formula = (
        f"{claim.theme_label} → 验 {claim.metric.label_cn}方向。{claim.metric.label_cn}"
        f"：{prev} 年 {series.get(prev)} → {period} 年 {series.get(period)}"
    )

    if direction == "flat":
        # ⚠ 持平既不是支持也不是冲突。判成冲突会凭空多出一处「叙事与事实相悖」，
        #   而这正是要拿去给评审看的那张表——不能为了凑数把它塞进任何一边。
        #   `_direction` 已经写好了理由（含具体变动幅度与阈值），照用。
        return Observation(
            claim, INCOMPARABLE,
            reason or f"{claim.metric.label_cn}未变动，不足以判断主张是否兑现",
            formula, inputs, actual,
        )

    matched = direction == claim.expect
    return Observation(
        claim=claim,
        state=SUPPORTED if matched else CONFLICTED,
        reason=(
            f"主张成立，{claim.metric.label_cn}确实上行"
            if matched
            else f"主张说向好，但 {claim.metric.label_cn}下行"
        ),
        formula=formula,
        inputs=inputs,
        actual=actual,
    )


def verify_all(
    claims: list[Claim],
    series_by_metric: dict[str, dict[str, Decimal | None]],
    *,
    min_rel_change: Decimal = DEFAULT_MIN_REL_CHANGE,
) -> list[Observation]:
    """批量验证。同一个指标只取一次序列，由调用方传进来。"""
    out: list[Observation] = []
    for claim in claims:
        series = series_by_metric.get(claim.metric.key)
        if series is None:
            out.append(
                Observation(
                    claim, MISSING,
                    f"本次没有取到「{claim.metric.label_cn}」的数据，无法验证",
                )
            )
            continue
        out.append(verify(claim, series, min_rel_change=min_rel_change))
    return out


# ------------------------------------------------------------------ 汇总


@dataclass(frozen=True)
class Verdict:
    """汇总。**只计数与陈述，不打分。**

    分数是 `docs/04-index-rules.md` 的事。这里硬要凑一个 0–100，
    等于把会计口径偷偷定在代码里，而手册一落地它就成了两套并存的标准。
    """

    total: int
    counts: dict[str, int]
    text: str
    #: 一句话结论，给时间线和卡片抬头用
    headline: str

    @property
    def has_conflict(self) -> bool:
        return self.counts.get(CONFLICTED, 0) > 0


def summarize(observations: list[Observation]) -> Verdict:
    """把观测汇成一句话。措辞随证据走，不预设结论。"""
    counts: dict[str, int] = {s: 0 for s in STATE_CN}
    for ob in observations:
        counts[ob.state] = counts.get(ob.state, 0) + 1
    total = len(observations)

    if total == 0:
        return Verdict(
            0, counts,
            text="没有从管理层讨论中识别出可验证的经营主张。"
                 "这通常意味着该年报的措辞较笼统，或主题词表未覆盖它的表述习惯——"
                 "**不代表公司没有问题**。",
            headline="无可验证主张",
        )

    n_ok, n_bad = counts[SUPPORTED], counts[CONFLICTED]
    usable = n_ok + n_bad
    bits = [f"{n_ok} 条被财务事实验证", f"{n_bad} 条与财务事实相悖"]
    other = total - usable
    if other:
        bits.append(f"{other} 条无法验证")

    if usable == 0:
        headline = "无法判定"
        lead = "识别到了主张，但没有一条能得到财务事实的验证——不出结论。"
    elif n_bad == 0:
        headline = "叙事与财务事实一致"
        lead = "识别到的可验证主张全部得到财务事实支持，未发现相悖表述。"
    else:
        headline = "存在叙事与事实相悖之处"
        lead = (
            f"有 {n_bad} 条主张与同期财务事实方向相反，"
            "建议逐条复核证据面板里的原句。"
        )

    tail = (
        "\n（本结论由规则法得出：按措辞命中识别主张，再比对财务指标方向。"
        "尚未接入 LLM 主张抽取，也不含诊断指数——指数公式待 docs/04 规则手册落地。）"
    )
    return Verdict(total, counts, text=f"{lead}\n识别到 {total} 条可验证主张：{'，'.join(bits)}。{tail}",
                   headline=headline)


__all__ = [
    "CONFLICTED", "INCOMPARABLE", "MISSING", "STATE_CN", "SUPPORTED",
    "Claim", "MetricRef", "Observation", "THEMES", "Theme", "Utterance",
    "Verdict", "find_claims", "summarize", "verify", "verify_all",
]
