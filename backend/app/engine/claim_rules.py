"""主张抽取的判定规则：主题、方向、幅度、期间。

纯函数，零 IO——文本由 `app/parsing/claims.py` 切好传进来。

主题表按会计口径 v1.1 §A.6 建。**五类里只实现三类**，另两类标
`implementable=False`：

    降本增效      ✅  主判据 operating_cost / 期间费用率
    需求与产销    ✅  主判据 钢材销量
    回款改善      ✅  主判据 DSO / CFO
    产品结构升级  ❌  主判据「高端产品销量占比」字段字典里没有
    产能释放      ❌  主判据 capacity_utilization 在库中 0 行

后两类**一律判 unverifiable 并写明原因**，不拿代理指标硬判。
v1.1 明确：主判据未披露时「标记不可验证或待核查，
**不能降级用代理指标直接作出冲突结论**」。用毛利率去判产品升级、
用收入去判回款，都是被点名的禁止简化——它们「看起来给出了结论」，
而那个结论没有任何证据支撑。

每类主题还带一份 `forbidden`（禁止的简化推断）。它不参与匹配，
但会写进判定理由——**佐证指标可以用来软化冲突，不能用来制造冲突**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Sequence

Direction = Literal["up", "down", "improve", "deteriorate", "flat", "unknown"]

# ---------------------------------------------------------------- 主题表


@dataclass(frozen=True)
class ThemeRule:
    theme: str
    label_cn: str
    #: 落库到 `claim.claim_type` 的值。**取值受该列的 CHECK 约束**，
    #: 与业务主题名不是一回事——两边对不上时 INSERT 会被数据库拒绝，
    #: 而拒绝发生在整批写入的中途，前面写进去的回滚、后面的全没写。
    claim_type: str
    #: 主判据的 metric_key。None 表示字典里没有合适的直接指标。
    primary_metric: str | None
    supporting: tuple[str, ...]
    trigger_terms: tuple[str, ...]
    #: 该主题**被禁止的简化推断**，写进判定理由，供人工复核时对照
    forbidden: tuple[str, ...]
    #: False 时无论文本多像，一律判 unverifiable
    implementable: bool
    unimplemented_reason: str = ""


THEMES: tuple[ThemeRule, ...] = (
    ThemeRule(
        theme="cost_reduction",
        label_cn="降本增效",
        claim_type="cost",
        primary_metric="operating_cost",
        supporting=("gross_margin", "admin_expense", "selling_expense", "rd_expense"),
        trigger_terms=(
            "降本", "增效", "成本", "费用", "降耗", "节能", "挖潜",
            "降本增效", "成本管控", "费用控制", "对标",
        ),
        forbidden=(
            "毛利率下降不等于降本失败——售价下降也会压缩毛利率",
            "整体毛利率不能单独证明或否定降本成效",
        ),
        implementable=True,
    ),
    ThemeRule(
        theme="demand_sales",
        label_cn="需求与产销",
        claim_type="demand",
        primary_metric="steel_sales_volume",
        supporting=("total_revenue", "revenue", "inventory", "contract_liabilities"),
        # ⚠ 「市场」「销售」这类词几乎命中一切句子（实测分别命中 97 / 143 句），
        # 而它们区分不出「这是一条关于产销的主张」还是「这句话里恰好出现了
        # 市场两个字」。触发词要么具体、要么带指标，宁缺毋滥——
        # 漏掉的可以由 LLM 版补，噪音则会让页面完全没法看。
        trigger_terms=(
            "销量", "产销", "需求", "订单", "产量", "去库存", "库存",
            "合同负债", "预收", "产销量", "销售量", "产能利用率",
        ),
        forbidden=(
            "收入下降可能由价格造成，不能直接用收入验证需求",
            "产量不能代替销量验证需求",
        ),
        implementable=True,
    ),
    ThemeRule(
        theme="collection",
        label_cn="回款改善",
        claim_type="collection",
        primary_metric="accounts_receivable",
        supporting=("cfo", "notes_receivable", "accounts_payable"),
        # ⚠ 刻意**不含「现金」「资金」**。它们太泛——实测「现金」命中 119 句，
        # 而现金流量表的每一行都带「现金」，于是「购建固定资产支付的现金」
        # 这类科目行全被归成了「回款改善」。那不是主张，是报表科目。
        trigger_terms=(
            "回款", "回笼", "应收账款", "应收票据", "账期", "现款",
            "货款", "票据结算", "周转天数", "应收账款周转",
        ),
        forbidden=(
            "CFO 下滑不能单独否定回款——存货采购与应付结算也会影响 CFO",
            "销售收现受增值税、预收款与票据结算影响，只在口径一致时比较",
        ),
        implementable=True,
    ),
    ThemeRule(
        theme="product_mix",
        label_cn="产品结构升级",
        claim_type="product_mix",
        primary_metric=None,
        supporting=("gross_margin", "rd_expense", "steel_price_avg"),
        trigger_terms=(
            "产品结构", "品种结构", "高端", "高附加值", "结构优化",
            "新产品", "首发的", "独有",
        ),
        forbidden=("整体毛利率不能单独证明或否定产品升级",),
        implementable=False,
        unimplemented_reason=(
            "主判据「高端/重点产品销量占比」需要按产品线拆分的销量与收入，"
            "字段字典里没有对应指标，本批数据也未按产品线采集。"
            "按 v1.1 不降级用毛利率等代理指标硬判。"
        ),
    ),
    ThemeRule(
        theme="capacity_release",
        label_cn="产能释放",
        claim_type="capacity",
        primary_metric="capacity_utilization",
        supporting=("steel_output", "cip", "ppe"),
        trigger_terms=("产能", "投产", "达产", "产能利用率", "释放", "在建工程", "转固"),
        forbidden=(
            "转固增加不等于产能已经释放",
            "设计产能不进分母，产能利用率一律取有效产能",
        ),
        implementable=False,
        unimplemented_reason=(
            "主判据 capacity_utilization 在库中 0 行——本批数据未采集产能利用率。"
            "按 v1.1 标不可验证，不用产量等代理指标硬判。"
        ),
    ),
    ThemeRule(
        theme="management_budget",
        label_cn="管理层预算",
        claim_type="management_budget",
        primary_metric="revenue",
        supporting=("total_revenue", "operating_cost", "steel_sales_volume"),
        # ⚠ **触发词刻意留空。** 认这一类的判据不是「句子里出现了某个词」，
        # 而是**它所在的位置**——在「N、20XX年经营计划」标题之下，加上指标名。
        # 给一组触发词的话 `match_theme` 会把年报里每一句「营业收入」都收进来，
        # 而其中绝大多数是**本年的实绩**，不是计划。
        # 所以它只由 `match_plan_budget()` 认，`match_theme` 永远命中不了它。
        trigger_terms=(),
        forbidden=(
            "计划值不是已发生的事实——拿当年的实际值去「核验」计划本身，"
            "等于拿事实核验事实，永远判支持",
            "只有**同一合并范围**下的公司总预算与实际总收入可比；"
            "子公司分项不能相加后当总额",
            "预算达成不等于预测准确，也不等于管理层可信",
        ),
        implementable=True,
    ),
)

#: 预算句里认的指标名。**必须整体字面命中**，不能只看「收入」两个字——
#: 「资金流量预算收入825.77亿元」「其中:经营收入419.35亿元」都含「收入」，
#: 但那是资金收支预算，不是经营目标。
_BUDGET_METRIC_TERMS: tuple[str, ...] = (
    "营业收入", "营业总收入", "主营业务收入",
)


def match_plan_budget(text: str, plan_year: str | None) -> ThemeMatch | None:
    """句子是不是「20XX 年经营计划」标题下的一条**公司总预算**。

    ★ 两类句子在原文里长得一模一样，靠词表分不开：

        2016年营业收入327.01亿元，同比下降11.9%。   ← 计划（上一年年报里写的）
        2022年公司实现营业收入1181.42亿元。          ← 实绩

    分开它们的是**位置**：前者在「N、20XX年经营计划」标题之下，后者在
    「主营业务分析」里。所以这个函数必须拿到 `plan_year`——由解析层在扫段落时
    从标题行里继承下来（见 `app/parsing/claims.py::iter_sentences`）。
    **拿不到计划年就返回 None，绝不猜。**

    ⚠ 取数时用的是 `_pick_by_metric`，它取的是主判据别名（「营业收入」）**同一分句内**
    的下一个带单位数字——所以

        2016年营业收入327.01亿元，…其中：迁钢公司137.32亿元，京唐公司178亿元…

    取到的是**总额 327.01 亿**，不是子公司分项。会计口径第 6 条要的正是这个
    （「不把子公司分项简单相加」）。
    """
    if not plan_year:
        return None
    hits = tuple(t for t in _BUDGET_METRIC_TERMS if t in text)
    if not hits:
        return None
    return ThemeMatch(
        rule=THEME_BY_KEY["management_budget"], matched_terms=hits, score=len(hits)
    )

THEME_BY_KEY = {t.theme: t for t in THEMES}


# ---------------------------------------------------------------- 方向

# 顺序有讲究：先匹配更具体的词。例如「同比下降」同时含「下降」，
# 而「降本」含「降」——若用单字匹配会把「降本」误判成下降。
_UP_TERMS = (
    "同比增长", "同比上升", "同比增长率", "增长", "上升", "增加", "提高",
    "上涨", "回升", "扩大", "提升", "向好", "创历史新高", "增产",
)
_DOWN_TERMS = (
    "同比下降", "同比减少", "下降", "减少", "降低", "下滑", "下跌",
    "收窄", "回落", "缩减", "减产",
)
_IMPROVE_TERMS = (
    "改善", "优化", "好转", "增强", "提升", "增效", "降本", "提高效率",
    "持续向好", "明显好转",
)
_DETERIORATE_TERMS = ("恶化", "变差", "加剧", "承压", "走弱", "形势严峻")

# 出现这些词时方向不可信：意向表述、否定、条件
_MODALITY_TERMS = (
    "努力", "力争", "拟", "计划", "争取", "有望", "预计", "旨在",
    "将会", "将要", "准备", "意图",
)
_NEGATION_TERMS = ("未", "没有", "未能", "无法", "不再", "难以")


def extract_direction(
    text: str, *, anchors: Sequence[str] = ()
) -> tuple[Direction, bool]:
    """判断句子的方向。返回 (方向, 是否为仅意向表述)。

    意向表述（「力争提升」「计划增长」）**不是保证承诺**，v1.1 明确
    「可以跟踪目标完成情况，但**不能据此推断虚假陈述**」。所以这里把它
    标出来，由调用方决定降低置信度，而不是当成实打实的承诺。

    `anchors` 传**主题的触发词**。传了以后，`improve` / `deteriorate`
    必须和主题的对象**在同一个分句里**才算数——理由见 `_anchored`。

    ⚠ 只对 `improve` / `deteriorate` 上这道闸门，**`up` / `down` 不上**。
    两者不是一类词：「提升」「优化」「改善」描述的是**某个名词的状态**，
    「核心竞争力显著提升」里的提升与成本无关；而「上升 9.6%」本身就是一个
    数值变化，主语就是整句在说的事，再要求同分句出现主题词会把
    「全年实现『1+1+N』产品销量3,059万吨，**同比上升9.6%**」这种
    刚认出来的真主张重新丢掉——那正是这一轮要救回来的东西。
    """
    modality_only = any(t in text for t in _MODALITY_TERMS)

    # 先看改善/恶化——它们描述的是「状态变好」而不是「数值变大」
    for term in _IMPROVE_TERMS:
        if term in text and _anchored(text, term, anchors):
            return "improve", modality_only
    for term in _DETERIORATE_TERMS:
        if term in text and _anchored(text, term, anchors):
            return "deteriorate", modality_only
    # 再看不带否定的增长/下降
    for term in _UP_TERMS:
        if not (term in text and not _negated(text, term)):
            continue
        # ⚠ 「提升」同时在两张表里：锚定已经在上面判过它不成立，
        #   这里必须**同样要求锚定**，否则它会从 `_UP_TERMS` 漏回来——
        #   「核心竞争力显著提升」照样会变成 `up`，等于闸门白装。
        if term in _AMBIGUOUS and not _anchored(text, term, anchors):
            continue
        return "up", modality_only
    for term in _DOWN_TERMS:
        if not (term in text and not _negated(text, term)):
            continue
        if term in _AMBIGUOUS and not _anchored(text, term, anchors):
            continue
        return "down", modality_only
    if "持平" in text or "基本稳定" in text or "保持稳定" in text:
        return "flat", modality_only
    return "unknown", modality_only


#: 同时能当「状态改善」和「数值上升」讲的词，**按更严的那条处理**。
#:
#: ⚠ 不能只做集合求交：`_IMPROVE_TERMS` 里有「持续向好」、`_UP_TERMS` 里有
#: 「向好」，`in` 判断下前者命中时后者也命中，而两者是不同的字符串。
#: 所以还要把**互为子串**的也算进来。
_AMBIGUOUS = frozenset(
    t
    for t in (*_UP_TERMS, *_DOWN_TERMS)
    if any(t in i or i in t for i in (*_IMPROVE_TERMS, *_DETERIORATE_TERMS))
)


_CLAUSE_BREAK = "。！？；，,;"


def _anchored(text: str, term: str, anchors: Sequence[str]) -> bool:
    """`term` 所在的分句里有没有主题的对象（`anchors`）。

    ★ 为什么需要这一条。宝钢 2021/2022 年报里有一批这样的句子：

        2021年，公司持续深化改革，全面对标找差，打造极致效率，
        一公司多基地协同优势进一步显现，**核心竞争力显著提升**……

    它含「成本」类的主题词（「对标找差」「降本」），被归进「降本增效」，
    方向词是「提升」→ `improve`；而 `improve` 对营业成本的含义是
    **成本下降**。于是「核心竞争力提升」被判成「相悖」——
    理由是「营业成本实际上升」。实测这一类在宝钢**相悖里占了大头**，
    而它们说的是效率、竞争力、地位，**根本不是关于成本的主张**。

    ⚠ **往前多看一个分句。** 中文是话题链，对象常常在上一个分句里点过就不再说：

        其中，全年实现销量115万吨，其中汽车板销量92.55万吨，占比进一步提升至80%；

    「提升」所在的分句只有「占比」，对象「销量」在上一个分句。
    只看本分句会把它判成「没有方向」——**而它明明是在说销量提升了**。

    ⚠ 但**只能多看一个**，不能放宽到整句。反例是这一句：

        ……全面对标找差，打造极致效率，一公司多基地协同优势进一步显现，
        核心竞争力显著提升，国内碳钢板材领导地位进一步强化。

    整句里到处都是「对标」（降本主题的触发词），放宽到整句会把
    「核心竞争力提升」重新读成「成本下降」——那正是这条闸门要挡的东西。
    紧前一个分句里没有「对标」，所以锚定到那里是安全的。

    ⚠ **没给 `anchors` 一律放行**，同 `_pick_by_metric` 的
    「没查不等于对不上」：调用方没给主题词表时，我们没有依据说它错位。
    """
    if not anchors:
        return True
    at = text.find(term)
    if at < 0:
        return False

    def _clause_before(pos: int) -> tuple[int, int]:
        """pos 所在分句的 [起, 止)。"""
        start = max((text.rfind(b, 0, pos) for b in _CLAUSE_BREAK), default=-1) + 1
        ends = [e for e in (text.find(b, pos) for b in _CLAUSE_BREAK) if e >= 0]
        return start, (min(ends) if ends else len(text))

    start, end = _clause_before(at)
    if any(a in text[start:end] for a in anchors):
        return True
    # 紧前一个分句。`start - 1` 是分隔符本身，从它再往前找上一个分句的起点。
    if start > 0:
        prev_start, prev_end = _clause_before(start - 1)
        if prev_end == start - 1 and any(a in text[prev_start:prev_end] for a in anchors):
            return True
    return False


def _negated(text: str, term: str) -> bool:
    """术语所在**分句**内、术语之前出现否定词，就算是否定。

    「未能实现销量增长」的语义与字面相反，直接按词面判会得到完全相反的
    结论——而且不报错。

    ⚠ 不能用「前 N 个字」的窗口：中文里否定词和动词之间常隔着宾语
    （「未能实现**销量**增长」，隔了 6 个字），窗口太小会漏判；
    窗口开大又会误判（「公司未发生重大变化，销量增长 8%」里那个「未」
    管不到后面的「增长」）。

    按分句切就两个问题都没有：否定只在同一分句内起作用，
    而「，」正是分句边界。
    """
    at = text.find(term)
    if at < 0:
        return False
    start = max((text.rfind(b, 0, at) for b in _CLAUSE_BREAK), default=-1) + 1
    return any(neg in text[start:at] for neg in _NEGATION_TERMS)


# ---------------------------------------------------------------- 幅度


@dataclass(frozen=True)
class Magnitude:
    """主张里的数值目标或幅度表述。"""

    raw: str
    value: Decimal
    unit: str            # '%' / '个百分点' / '亿元' / ...
    bound: Literal["exact", "at_least", "at_most", "about"]
    #: 这个数字是不是**计划值**（所在分句里出现「计划 / 预算 / 目标」这类词）。
    #:
    #: ⚠ 这一位决定判定走第 4 条还是第 6 条，不是装饰：
    #:
    #:     已发生的事实  「2022年公司销售商品坯材4,976.3万吨」
    #:        → 拿它去核验等于**拿事实核验事实**，永远判「支持」。
    #:          而假的「支持」会把 H 和 C 一起抬上去，还看不出来。
    #:     计划值        「2018年公司计划营业成本2,420亿元」
    #:        → 和当年实际比，实际 2,590.85 亿，超支 7%，是实打实的未达成。
    #:
    #: 两者的区别**只在有没有计划模态词**，所以必须在抽取时记下来——
    #: 判定那一步手上只有这一行数据，回头去原文找「计划」二字是找不到的。
    is_plan: bool = False
    #: 这个数是不是**从主判据别名旁边**取来的。
    #:
    #: ⚠ 这一位是「能不能拿它和主判据比」的前提，不是装饰。
    #: `_pick_by_metric` 找不到别名时会**退回取第一个带单位的数**——
    #: 那意味着这句话里的数字与主判据没有文字上的关联。真实撞到过：
    #:
    #:     2024年，公司预算安排固定资产投资资金239.2亿元，主要用于……
    #:
    #: 主张的主题被映射成 `operating_cost`，句子里的 239.2 亿元其实是**资本开支**。
    #: 拿它和营业成本（2,809 亿元）比，偏差 +280,626 百万元，按 8-2 的成本方向
    #: 算出来是「未达成」——**有数字、有理由、有公式，和真结论长得一模一样**。
    #:
    #: 所以只有 `True` 才允许把目标与主判据比。这正好对上会计口径 8-3 第 1 条
    #: 要求的「先核对业务范围和期间」——别名取到的数，业务范围才是有据的。
    metric_aligned: bool = True

    @property
    def is_ratio(self) -> bool:
        return self.unit in ("%", "％", "个百分点")


# 数值 + 单位。单位按长度排，避免「个百分点」被拆成「个」。
# **必须支持千位分隔符**——年报里的金额几乎都写成 4,976.3 这种形式，
# 不认逗号的话只能匹配到开头的「4」，幅度整体错三个数量级且不报错。
_MAGNITUDE_RE = re.compile(
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>个百分点|百万元|万亿元|亿元|万元|千元|元|亿吨|万吨|吨|%|％|倍)?"
)

# ⚠ 顺序要紧：**否定式必须排在肯定式之前**。
# 「不超过」里含「超过」、「不高于」里含「高于」——先查肯定式的话，
# 「下降不超过 10%」会被判成「至少下降 10%」，方向完全相反。
_AT_MOST = ("不超过", "不高于", "以内", "以下", "最多")
_AT_LEAST = ("不低于", "不少于", "以上", "至少", "超过", "超")
_ABOUT = ("约", "左右", "大约", "近", "逾", "余")

# 判定边界时往前看几个字。「不低于」「不超过」都是三个字，
# 只看两个字会把「不低于」截成「低于」而漏判。
_BOUND_LOOKBACK = 4


#: 年份区间写法：「2019-2021年」「2019—2021年」「2019至2021年」。
#: 区间**两头的数字都是年份**，不能只放行第一个、把第二个当幅度。
_YEAR_RANGE_RE = re.compile(r"\s*[-–—~～至]\s*20\d{2}\s*年")


def _is_year(match: re.Match[str], text: str) -> bool:
    """这个数字是不是年份。

    句子几乎都以「2022 年」开头，直接取第一个数字的话，
    幅度会全被解析成「2022」——句子里的真实金额（4,976.3 万吨）
    反而被丢掉，而且**不报错**：判定会拿「2022」去和实际值比，
    得到一个永远对不上的偏差，最后把好句子判成冲突。

    区间写法要单独认：宝钢的「公司2019-2021年规划目标」里，
    `2019` 后面跟的是减号不是「年」，只判「紧跟年字」会把它当成幅度，
    于是幅度变成 2019——一个既不是金额也不是数量的数。
    """
    if match.group("unit"):
        return False
    try:
        value = int(match.group("num"))
    except ValueError:
        return False
    if not (1900 <= value <= 2099):
        return False
    tail = text[match.end() : match.end() + 1]
    if tail == "年":
        return True
    return bool(_YEAR_RANGE_RE.match(text[match.end() :]))


#: 计划模态词。数字所在分句里出现这些词，才算「计划值」而不是已发生的事实。
#:
#: ⚠ 刻意**不含**「预计 / 有望 / 力争」——那是预测与意向，v1.1 明确
#: 「可以跟踪目标完成情况，但不能据此推断虚假陈述」，核验口径不同。
_PLAN_TERMS = ("计划", "预算", "目标", "安排", "拟定", "拟")

#: 断分句时用的边界。⚠ **「、」不在里面**，这一点是有意的：
#: 宝钢的年度经营计划是「2018年，公司计划产铁X万吨、产钢Y万吨、…、营业成本Z亿元」——
#: 「计划」在句首，管的是整个顿号列表。若把「、」也当边界，
#: 「营业成本Z亿元」那个分句里就没有「计划」二字，最该认出来的目标反而认不出。
_PLAN_CLAUSE_BREAK = "。！？；，"


def _plan_clause(text: str, pos: int) -> str:
    """取 pos 所在的那个「强分句」——回退到上一个句号/分号/逗号为止。

    ⚠ 硬边界是 `_PLAN_CLAUSE_BREAK` 里那几个字符，**不含顿号**，理由见那里的说明。
    """
    start = max((text.rfind(b, 0, pos) for b in _PLAN_CLAUSE_BREAK), default=-1) + 1
    return text[start : pos + 1]


def _crosses_clause(text: str, start: int, end: int) -> bool:
    """`text[start:end]` 之间有没有分句边界。"""
    return any(b in text[start:end] for b in _PLAN_CLAUSE_BREAK)


def _pick_by_metric(
    matches: list[re.Match[str]], text: str, metric_aliases: Sequence[str]
) -> tuple[re.Match[str], bool]:
    """一句话里有多个数字时，挑出**与主判据指标对应的**那一个。

    ⚠ 宝钢每年的「年度经营计划」一句话里塞五六个目标：

        2018年，宝钢股份计划产铁4563万吨、产钢4737万吨、销售商品坯材4568万吨、
        营业总收入2786亿元、营业成本2420亿元。

    主判据是 `operating_cost`，而「取第一个带单位的数」拿到的是
    **4,563 万吨（产铁）**——拿铁产量去和营业成本比，量纲完全不同，
    算出来的偏差毫无意义，然后判成「未达成」。**不报错。**

    做法：找到主判据别名（「营业成本」）的位置，取它**同一分句内**的下一个
    带单位数字。别名的来源是 `metric_definition.aliases`——那份字典是
    「PDF 行名 → 字段键」映射的唯一依据，这里复用它，不另造一份词表。

    返回 `(数字, 是不是靠别名取到的)`。

    ⚠ **第二个值必须往上传，不能在这里丢掉**（和 `bound` 当年一样）。
    退回取第一个带单位数字时，这句话的数**与主判据没有文字上的关联**——
    真实撞到过「预算安排固定资产投资资金239.2亿元」被当成营业成本目标。
    丢了这一位，判定层就分不出「有据的对应」和「碰巧挨着的一个数」，
    只能照判，而判出来的东西有数字有理由、看着像真的。
    """
    for alias in metric_aliases:
        if not alias:
            continue
        at = text.find(alias)
        if at < 0:
            continue
        after = at + len(alias)
        # 别名与数字之间不许跨分句边界——否则「营业成本同比下降，销量 3000 万吨」
        # 会拿「3000 万吨」去当营业成本的目标。
        for m in matches:
            if (
                m.group("unit")
                and m.start() >= after
                and not _crosses_clause(text, after, m.start())
            ):
                return m, True
    fallback = next((m for m in matches if m.group("unit")), matches[0])
    # ⚠ **「没查」不等于「对不上」。** 一个别名都没传（没给字典数据，
    # 或者这个指标在字典里就没写别名）时，我们没有依据说它错位——
    # 报 False 会让一整类主张被静默降级成待核查。
    if not any(metric_aliases):
        return fallback, True
    return fallback, False


def extract_magnitude(
    text: str, metric_aliases: Sequence[str] = ()
) -> Magnitude | None:
    """抽取句子里明确的数值目标或幅度。

    优先取**带单位的**数字——年报里的金额与数量几乎都带单位，
    而年份不带。只有在没有带单位数字时才退回取不带单位的。

    `metric_aliases` 传入主判据指标的别名（取自字段字典），
    用来在一句多目标时挑对那个数；不传则退回「第一个带单位的」。

    没有数字的方向性表述（「销量持续增长」）返回 None——
    v1.1 要求这类只做「方向级验证」，不套用任何幅度阈值。
    """
    matches = [m for m in _MAGNITUDE_RE.finditer(text) if not _is_year(m, text)]
    if not matches:
        return None

    chosen, aligned = _pick_by_metric(matches, text, metric_aliases)
    unit = chosen.group("unit") or ""
    try:
        # ⚠ **必须去掉千位分隔符**：Decimal("4,976.3") 会抛 InvalidOperation，
        # 而这个 except 会把它吞成 None——「有幅度」于是变成「没幅度」，
        # 静默地。年报里的金额几乎都带逗号，这个坑会吃掉绝大多数金额。
        value = Decimal(chosen.group("num").replace(",", ""))
    except InvalidOperation:      # pragma: no cover - 正则保证是数字
        return None

    span = text[max(0, chosen.start() - _BOUND_LOOKBACK) : chosen.end() + 3]
    if any(w in span for w in _AT_MOST):
        bound: Literal["exact", "at_least", "at_most", "about"] = "at_most"
    elif any(w in span for w in _AT_LEAST):
        bound = "at_least"
    elif any(w in span for w in _ABOUT):
        bound = "about"
    else:
        bound = "exact"

    # 起点用 chosen.start() + 1：「2022年公司实现销量4,976万吨」里的
    # 「实现」在数字之前、且跨不过逗号，不会被误当成计划词。
    is_plan = any(t in _plan_clause(text, chosen.start() + 1) for t in _PLAN_TERMS)

    return Magnitude(
        raw=chosen.group(0), value=value, unit=unit, bound=bound, is_plan=is_plan,
        metric_aligned=aligned,
    )


# ---------------------------------------------------------------- 期间

# 明确的四位数年份。**必须带「年」字，不能写成 `年?`。**
#
# ⚠ 可选的那个「年」是真实存在的 bug：「……材2073万吨，同比降低6.5%」
# （首钢 2023 年报原句）里的 2073 会被当成 2073 年，于是这条主张变成
# 一条 50 年后才到期的前瞻计划——永远进不了验证、永远不判冲突，
# 却实实在在占用 H 的观测集。**不报错**，只是 H 里混进一条永远不动的。
_YEAR_RE = re.compile(r"(20\d{2})\s*年")

#: 期间表述 → 相对报告年的偏移。**顺序有意义，长的排前面**：
#: 元组顺序决定了谁先命中，写成字典靠插入顺序是隐式的、改一处就会静默变味。
_RELATIVE: tuple[tuple[str, int], ...] = (
    # ---- 指报告年 ----
    # 「全年」是后补的。原来只认「本期 / 本年度 / 报告期 / 当期」，
    # 于是「全年实现『1+1+N』产品销量3,059万吨，同比上升9.6%」这类
    # **公司自己的、带数值带方向的**句子一条都进不了判定——
    # 年报里「全年」就是报告年，没有第二种解释。
    ("本报告期", 0), ("本年度", 0), ("报告期", 0), ("本期", 0),
    ("本年", 0), ("当期", 0), ("全年", 0),
    # ---- 指上一年 ----
    ("上年度", -1), ("上年", -1), ("去年", -1), ("同期", -1),
    # ---- 指下一年 ----
    ("下一年", 1), ("明年", 1), ("次年", 1),
)

#: **比较词**：说的是「和上一年同期比」，而主张本身的期间仍是**报告年**。
#:
#: ⚠ 与 `_RELATIVE` 分开，是因为它们多一道闸门。年报里「同比」几乎只出现在
#: 两种句子里：一种是讲本年的实绩（「汽车板产量439.4万吨，同比增长约9%」），
#: 一种是把**下一年**的展望说出来（「预计基建用钢需求同比有望增长」）。
#: 后者的「同比」指的不是报告年，锚错了会让一条前瞻主张拿当年的实际值去判——
#: 判出来的东西有数字、有理由、有公式，**唯独年份是错的，而看不出来**。
#: 所以句中出现前瞻表述（`_FORWARD_LOOKING`）时比较词不锚定。
#:
#: ⚠ **「环比」刻意不收。** 它在宝钢的年报里只出现在「成本环比削减X亿元」
#: 这类条目上，而那一串条目里**实绩与下一年目标混排**：
#: 「与年度经营目标比，2022年公司…成本环比削减93.5亿元」（本年实绩）与
#: 「2023年…努力实现"…成本环比削减29亿元以上"」（下一年目标）长得一模一样，
#: 目标年写在段落标题「2.2023年公司经营目标、计划与拟开展的重点工作」里，
#: 按句子切完就丢了。锚到报告年会把下一年目标当成本年实绩去判。
#: **判不了就不判**——这一批留在「不可验证」，别名不猜。
_COMPARATIVE: tuple[str, ...] = (
    "比上年同期", "比去年同期", "较上年同期", "同比",
)

#: 前瞻表述。出现这些时，句中那些**本身不指年份**的期间词不锚定。
#:
#: ⚠ **刻意比 `_MODALITY_TERMS` 窄。** 那一个还含「准备」，而年报里
#: 「计提资产减值**准备**」「**准备**金」遍地都是——拿它当硬闸门，
#: 「2017年公司实现净利润204.0亿元……全年经营应得现金410.2亿元」
#: 会被当成前瞻句挡掉。`_MODALITY_TERMS` 只用来给方向降置信度，那里
#: 误判的代价是小数变了一点点；这里误判的代价是一条主张凭空消失。
_FORWARD_LOOKING: tuple[str, ...] = (
    "预计", "有望", "力争", "计划", "拟将", "将会", "将要",
    "争取", "旨在", "目标", "展望",
)

#: `_RELATIVE` 里**需要前瞻闸门**的那些词。
#:
#: 「本期 / 本年度 / 报告期」自带指向性，说的是「这一份报告的那一年」，
#: 前瞻句里用它们也不会指错。**「全年」不一样**——它只说「一整年」，
#: 「预计全年粗钢产量将有所下降」在 2024 年报里说的是 **2025**。
_FORWARD_GUARDED: frozenset[str] = frozenset({"全年"})


def resolve_period(
    text: str,
    report_period: str,
    *,
    forward_verifies_next_year: bool = True,
) -> str | None:
    """从原文的期间表述推出**目标期间**。

    ★ 这是最要紧的一条规则，也是最容易搞反的：

        2023 年年报里说「2024 年公司计划……」→ 目标期间是 **2024**，
        用 2024 年的实际结果去验证。

    搞反了会产生**事后偏见的「处处支持」**——拿已经发生的事实去「验证」
    与之同时写下的表述，当然处处对上。而且这个错误**不报任何错**，
    只会让指数虚高。docs/00 §八 把未来信息泄漏列为评测边界之一。

    `forward_verifies_next_year` 来自 rule_config，置 0 表示不做下一年映射。
    """
    explicit = _YEAR_RE.search(text)
    if explicit:
        return explicit.group(1)

    try:
        base = int(report_period[:4])
    except (ValueError, TypeError):
        return None

    forward = any(t in text for t in _FORWARD_LOOKING)
    for word, delta in _RELATIVE:
        if word not in text:
            continue
        if word in _FORWARD_GUARDED and forward:
            continue
        if delta == 1 and not forward_verifies_next_year:
            return None
        return str(base + delta)

    # 比较词：只在句子**没有前瞻表述**时才认。
    if not forward:
        for word in _COMPARATIVE:
            if word in text:
                return str(base)
    return None


def extract_period_expr(text: str) -> str | None:
    """取出**原文里的期间表述**（「2024 年」「明年」「本期」），原样保留。

    与 `period_norm` 分开存：归一化期间用于匹配财务事实，
    原始表述用于在页面上显示「系统是照哪句话判的」。
    只存归一化值的话，人工复核时看不出它到底对应原文的哪几个字。
    """
    m = _YEAR_RE.search(text)
    if m:
        # 归一成「YYYY年」，不带出原文里的空白
        return f"{m.group(1)}年"
    for word, _ in _RELATIVE:
        if word in text:
            return word
    for word in _COMPARATIVE:
        if word in text:
            return word
    return None


# ---------------------------------------------------------------- 口径

#: 行业整体口径的**数据来源标记**。
#:
#: ⚠ 光看机构名（「中钢协」）不够：「首钢股份…被工信部科技司及**中钢协**
#: 评为优秀案例」里它是颁奖方，「按照湖南省科技厅、**统计局**等主管部门
#: 要求调整研发费用的归集范围」里它是主管部门——两处都跟行业数据无关，
#: 按机构名判会把这些**公司自己的**句子一起挡掉。所以要求机构名后面
#: 紧跟「统计 / 数据显示 / 发布 / 测算」这类**在报数**的说法。
_INDUSTRY_SOURCE_RE = re.compile(
    r"(?:中钢协|中国钢铁工业协会|国家统计局|统计局|钢联资讯|海关总署|wind资讯)"
    r"[^，。；]{0,10}?(?:统计|数据|发布|公布|显示|测算|监测)"
    r"|据(?:中钢协|中国钢铁工业协会|国家统计局|钢联资讯|海关总署)"
)

#: 整体口径的**主语 + 总量名词**：「我国粗钢产量」「国内生产总值」
#: 「全国规模以上企业工业增加值」。两个词必须挨在一起。
#:
#: ⚠ 不写「行业」单独一个词：「铁水成本保持行业前十」「行业引领」都是
#: 公司自己的话。也不写「钢铁行业整体」：「受益于钢铁行业整体盈利水平
#: 的改善，公司下属子公司…实现净利润 26.09 亿元」的主语是**公司自己**，
#: 行业只是一句背景。
#:
#: ⚠ 也不收「汽车 / 家电」这类下游行业词：「国内汽车板市场占有率」是公司
#: 自己的口径，收了会把宝钢销量类的句子一起挡掉。实测它们在语料里
#: 一条也没多挡住什么，去掉不亏。
_INDUSTRY_SCOPE_RE = re.compile(
    r"(?:我国|全国|国内|中国|全行业|全球)"
    r"(?:规模以上[^，。；、]{0,8})?"
    r"(?:粗钢|钢材|生铁|焦炭|水泥|有色金属|发电|原煤|"
    r"工业增加值|生产总值|经济总量|经济)"
)


def industry_scope(text: str) -> str | None:
    """这段话讲的是不是**全行业**，而不是这家公司。命中返回一句理由。

    ★ 为什么必须有这一条。`resolve_period` 补上「全年」之后，

        全年我国粗钢产量10.1亿吨，同比下降1.7%；

    会拿到报告年，随后被拿去和**宝钢自己的钢材销量**比。两个数一个是全国
    的、一个是公司的，比出来的方向却常常**一致**（都跟着钢周期走），
    判成「支持」，页面上看不出任何异常。**比错对象而结果看起来合理，
    比重错更危险**——它会被当成一条有力的证据。

    ⚠ 判据要**按段落**用，不能只看这一句：中文省略主语太普遍了。

        国家统计局数据显示，2024年中国粗钢产量10.05亿吨，同比下降1.7%；
        钢材产量14.00亿吨，同比增长1.1%。

    后半句单独拿出来，一个整体口径词都没有。
    """
    m = _INDUSTRY_SOURCE_RE.search(text)
    if m:
        return f"这段话引的是「{m.group(0)}」发布的行业数据，不是公司自己的数。"
    m = _INDUSTRY_SCOPE_RE.search(text)
    if m:
        return f"「{m.group(0)}」是全行业口径，不是公司自己的数。"
    return None


def is_pending(target_period: str | None, known_periods: tuple[str, ...]) -> bool:
    """目标期间还没到（不是已知的任何一年）。

    未到期的计划**不进覆盖率分母**，单独披露——它在逻辑上无法判定，
    算进去只会稀释覆盖率、把 n/N 压到闸门以下。
    """
    if target_period is None:
        return False
    return target_period not in known_periods


# ---------------------------------------------------------------- 匹配


@dataclass(frozen=True)
class ThemeMatch:
    """一句文本与某主题的匹配结果。"""

    rule: ThemeRule
    matched_terms: tuple[str, ...]
    score: float

    @property
    def usable(self) -> bool:
        return self.rule.implementable


#: 触发词被**跨词拼出来**的坑。键是触发词，值是（左邻字, 右邻字）——
#: 两者同时命中才算它落在那个更大的词里，这一处出现就不作数。
_TRIGGER_GUARD: dict[str, tuple[str, str]] = {
    # 「产销」不许命中「生**产销**售」
    "产销": ("生", "售"),
}


def _occurs(text: str, term: str) -> bool:
    """`term` 是不是真的作为一个词出现过。

    ★ 触发词是按**子串**匹配的，而中文没有词边界。实测：

        主要经营范围为化工原料及产品的**生产销售**……

    这句子公司经营范围的套话里有「生产销售」，「产销」**跨着「生产」和
    「销售」两个字**被拼了出来，于是它被归进「需求与产销」，
    并有 9 条主张因此进了判定——**而这句和产销毫无关系**。

    同「否定词不能用单字」是同一类坑：**命中读起来完全正常，
    只有把括号填回去才看得出它是拼的**。

    ⚠ 不能简单地把「产销」从词表里删掉——「产销量」「产销协同」
    「产销平衡」都是真主张。要挡的是**跨词那一种**。
    """
    guard = _TRIGGER_GUARD.get(term)
    if guard is None:
        return term in text
    left, right = guard
    start = 0
    while True:
        at = text.find(term, start)
        if at < 0:
            return False
        before = text[at - 1] if at > 0 else ""
        after = text[at + len(term)] if at + len(term) < len(text) else ""
        if not (before == left and after == right):
            return True
        start = at + 1


def match_theme(text: str) -> ThemeMatch | None:
    """找出句子对应的主题。命中词最多者胜，同分时取主题表里靠前的。

    ⚠ 返回 `usable=False` 的主题**不是「没匹配上」**，而是「匹配上了但
    没有直接指标可验证」。调用方必须把这种标 `unverifiable` 并写明原因，
    不能当成无主题丢弃——那会把「我们缺字段」伪装成「年报没提这件事」。
    """
    best: ThemeMatch | None = None
    for rule in THEMES:
        hits = tuple(t for t in rule.trigger_terms if _occurs(text, t))
        if not hits:
            continue
        score = len(hits)
        if best is None or score > best.score:
            best = ThemeMatch(rule=rule, matched_terms=hits, score=score)
    return best
