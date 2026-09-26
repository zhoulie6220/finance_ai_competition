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
from typing import Literal

Direction = Literal["up", "down", "improve", "deteriorate", "flat", "unknown"]

# ---------------------------------------------------------------- 主题表


@dataclass(frozen=True)
class ThemeRule:
    theme: str
    label_cn: str
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
        primary_metric="steel_sales_volume",
        supporting=("total_revenue", "revenue", "inventory", "contract_liabilities"),
        trigger_terms=(
            "销量", "产销", "需求", "订单", "产量", "市场", "销售",
            "去库存", "库存", "合同负债", "预收",
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
        primary_metric="accounts_receivable",
        supporting=("cfo", "notes_receivable", "accounts_payable"),
        trigger_terms=(
            "回款", "回笼", "应收账款", "账期", "现金", "现款", "票据",
            "货款", "资金", "周转",
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


def extract_direction(text: str) -> tuple[Direction, bool]:
    """判断句子的方向。返回 (方向, 是否为仅意向表述)。

    意向表述（「力争提升」「计划增长」）**不是保证承诺**，v1.1 明确
    「可以跟踪目标完成情况，但**不能据此推断虚假陈述**」。所以这里把它
    标出来，由调用方决定降低置信度，而不是当成实打实的承诺。
    """
    modality_only = any(t in text for t in _MODALITY_TERMS)

    # 先看改善/恶化——它们描述的是「状态变好」而不是「数值变大」
    for term in _IMPROVE_TERMS:
        if term in text:
            return "improve", modality_only
    for term in _DETERIORATE_TERMS:
        if term in text:
            return "deteriorate", modality_only
    # 再看不带否定的增长/下降
    for term in _UP_TERMS:
        if term in text and not _negated(text, term):
            return "up", modality_only
    for term in _DOWN_TERMS:
        if term in text and not _negated(text, term):
            return "down", modality_only
    if "持平" in text or "基本稳定" in text or "保持稳定" in text:
        return "flat", modality_only
    return "unknown", modality_only


_CLAUSE_BREAK = "。！？；，,;"


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


def _is_year(match: re.Match[str], text: str) -> bool:
    """这个数字是不是年份。

    句子几乎都以「2022 年」开头，直接取第一个数字的话，
    幅度会全被解析成「2022」——句子里的真实金额（4,976.3 万吨）
    反而被丢掉，而且**不报错**：判定会拿「2022」去和实际值比，
    得到一个永远对不上的偏差，最后把好句子判成冲突。
    """
    if match.group("unit"):
        return False
    try:
        value = int(match.group("num"))
    except ValueError:
        return False
    tail = text[match.end() : match.end() + 1]
    return tail == "年" and 1900 <= value <= 2099


def extract_magnitude(text: str) -> Magnitude | None:
    """抽取句子里明确的数值目标或幅度。

    优先取**带单位的**数字——年报里的金额与数量几乎都带单位，
    而年份不带。只有在没有带单位数字时才退回取不带单位的。

    没有数字的方向性表述（「销量持续增长」）返回 None——
    v1.1 要求这类只做「方向级验证」，不套用任何幅度阈值。
    """
    matches = [m for m in _MAGNITUDE_RE.finditer(text) if not _is_year(m, text)]
    if not matches:
        return None

    chosen = next((m for m in matches if m.group("unit")), matches[0])
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

    return Magnitude(raw=chosen.group(0), value=value, unit=unit, bound=bound)


# ---------------------------------------------------------------- 期间

# 明确的四位数年份
_YEAR_RE = re.compile(r"(20\d{2})\s*年?")
# 相对期间
_RELATIVE = {
    "本期": 0, "本年度": 0, "本年": 0, "报告期": 0, "当期": 0,
    "上年": -1, "上年度": -1, "去年": -1, "同期": -1,
    "下一年": 1, "明年": 1, "次年": 1,
}


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

    for word, delta in _RELATIVE.items():
        if word in text:
            if delta == 1 and not forward_verifies_next_year:
                return None
            return str(base + delta)
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


def match_theme(text: str) -> ThemeMatch | None:
    """找出句子对应的主题。命中词最多者胜，同分时取主题表里靠前的。

    ⚠ 返回 `usable=False` 的主题**不是「没匹配上」**，而是「匹配上了但
    没有直接指标可验证」。调用方必须把这种标 `unverifiable` 并写明原因，
    不能当成无主题丢弃——那会把「我们缺字段」伪装成「年报没提这件事」。
    """
    best: ThemeMatch | None = None
    for rule in THEMES:
        hits = tuple(t for t in rule.trigger_terms if t in text)
        if not hits:
            continue
        score = len(hits)
        if best is None or score > best.score:
            best = ThemeMatch(rule=rule, matched_terms=hits, score=score)
    return best
