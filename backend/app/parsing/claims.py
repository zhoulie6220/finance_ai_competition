"""从 MD&A 正文里切出候选句子。

这是主张抽取的**取数层**（有 IO 的边界在 `app/skills/narrative.py`，
这里只有纯文本处理），也是整条链路里最容易静默出错的一环。

三个现实问题
------------
1. **PDF 提取会硬换行。** 原文「复苏的可能性较大」在库里是
   「复苏的可能\\n性较大」——切句前必须先把同段落内的行接起来，
   否则切出来的「句子」全是半截话，而它们照样能匹配上主题词、
   照样进库、照样参与评分，**不报任何错**。

2. **很大一部分正文是表格。** 年报的「主营业务分产品情况」是这样的：

       冷轧碳钢板卷      41,655   35,695   14.31   -

   不做剔除的话，`claim` 表会被这类数字行填满，N 虚高、判定全是胡话。

3. **标题行不是句子。** 「(一)行业竞争格局和发展趋势」没有谓语，
   抽出来只会污染主题匹配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 句末标点。中文年报里基本上只用。和；分号后面常接并列分句，也切。
SENTENCE_END = re.compile(r"(?<=[。！？；])")

# 标题行：以「一、」「(一)」「1.」「（1）」等开头，且**不含句末标点**
HEADING_RE = re.compile(
    r"^\s*(?:[（(]?[一二三四五六七八九十]+[）)、.]|[（(]?\d+[）)、.]|\d+[、.])"
    r"\s*[^。！？；]{0,40}$"
)

# 数字 token：带千位分隔符、小数、百分号、或纯整数
_NUMBER_RE = re.compile(r"[-−]?\d[\d,]*(?:\.\d+)?%?")

# **一个单元格**是纯数字（可带分隔符/百分号/负号/占位横杠）
_NUMERIC_CELL_RE = re.compile(r"^[-−—–\s]*(?:\d[\d,]*(?:\.\d+)?%?)?[-−—–\s]*$")

# 表格列之间的分隔：两个以上空格或制表符。**这是 PDF 表格的可靠特征**——
# 列是靠空白对齐的，而正文里的空格是词距，只会有单个空格。
_MULTI_SPACE = re.compile(r"\s{2,}|\t")

# 表格的**说明行**：数据来源、单位、附注。这类行自成一块，后面的正文不许接上去。
#
# ⚠ 不挡住的话会拼出**年报上根本不存在的句子**：
#
#     粗钢产量  CSPI月均数据来源：wind资讯公司把握国家供给侧结构改革、钢铁去产能的机遇…
#
# 三段（表头 / 数据来源 / 正文首句）接成一句，而 claim_text 是证据链的终点——
# 它必须是年报里真实存在的那一句，否则「点回原文」就点到了一段拼接物。
# 实测宝钢 130 条候选里 4 条是这种。
_TABLE_CAPTION_RE = re.compile(r"(数据来源|资料来源|单位[:：]|注[:：])")


@dataclass(frozen=True)
class Sentence:
    """一段正文里切出的一个候选句子。"""

    text: str
    index: int          # 在所属 section 内的序号，进 claim_id 的哈希
    offset: int         # 在原始文本里的起始位置，用于回溯与高亮
    #: 这一句所在的**整段**（`join_wrapped_lines` 接完折行之后的那一行）。
    #:
    #: ⚠ 不是装饰。中文省略主语太普遍，「全年我国粗钢产量10.1亿吨」
    #: 后面紧跟的「钢材产量14.00亿吨」单独看没有任何整体口径词——
    #: 只有回到段落里才看得出它说的还是全国的数。判定见
    #: `claim_rules.industry_scope`。
    #:
    #: 它**不进 `claim_id` 的哈希**（那里只有 项目|章节|句序|原文），
    #: 所以加这个字段不会让会计填好的表作废。
    context: str = ""


def join_wrapped_lines(text: str) -> str:
    """把 PDF 硬换行接回段落。

    只接**不是段落边界**的换行：标题行的下一行另起一段，
    列表项、空行也都保留。判断依据是行尾没有句末标点且下一行不以
    标题/列表符号开头——那说明这行是被 PDF 宽度截断的，不是作者换的行。
    """
    lines = text.split("\n")
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line:
            out.append("")
            continue
        prev = out[-1] if out else ""
        if (
            prev
            and prev.strip()
            and not prev.rstrip().endswith(("。", "！", "？", "；", "："))
            # ⚠ **表格行与标题行都是完整的块，不能往里接。**
            #
            # 两者末尾都没有句末标点，所以下面的条件都会认为「可以接」：
            #   · 表格行接上正文后，数字占比被稀释，后面的剔除器再也认不出它
            #     （实测：占比从 0.89 掉到 0.45，整串数字进了候选句）
            #   · 标题行接上正文后，抽出来的「原句」会带着标题一起进 claim 表，
            #     而 claim_text 是证据链的终点，它必须是年报里真实存在的那一句
            and not looks_like_table_row(prev)
            and not HEADING_RE.match(prev.strip())
            # 表格说明行也是完整的块，理由见 _TABLE_CAPTION_RE
            and not _TABLE_CAPTION_RE.search(prev)
            and not _starts_new_block(line)
        ):
            out[-1] = prev + line.lstrip()
        else:
            out.append(line)
    return "\n".join(out)


def _starts_new_block(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if HEADING_RE.match(stripped):
        return True
    # 表格的说明行（数据来源 / 单位 / 注）自起一行，理由见 _TABLE_CAPTION_RE
    if _TABLE_CAPTION_RE.match(stripped):
        return True
    # 表格行的各列之间有多空格，不该和上一行接起来
    return bool(_MULTI_SPACE.search(stripped))


def looks_like_table_row(line: str) -> bool:
    """判断一行是不是表格行。

    ⚠ **判据必须靠 PDF 的列对齐（两个以上空格），不能靠「数字占比」。**

    最初用「数字 token 占 token 总数的比例」，结果把这类真实句子判成了表格行：

        中钢协统计数据显示，2022 年全国粗钢产量 10.13 亿吨，同比下降 2.1%…
        会员钢铁企业全年累计实现利润总额 982 亿元，同比下降 72.27%…

    这些恰恰是**最该抽取的句子**（带数值、带方向、带对象）。中文句子里
    数字被标点和空格隔开，占比自然就高——按比例判会把最有价值的句子
    全部丢掉，而且是**静默丢掉**：claim 表里少了一批主张，
    N 变小、覆盖率变化，没有任何地方报错。

    表格行真正的特征是**列用空白对齐**：

        冷轧碳钢板卷        41,655    35,695    14.31    -

    标签单元格 + 一串纯数字单元格。正文里的空格是词距，只会有单个空格。
    """
    stripped = line.strip()
    if not stripped:
        return False

    cells = [c for c in _MULTI_SPACE.split(stripped) if c.strip()]
    if len(cells) >= 3:
        numeric = sum(1 for c in cells if _NUMERIC_CELL_RE.match(c))
        # 最多允许一个非数字单元格（通常是行标签）
        if numeric >= len(cells) - 1:
            return True

    # 两格的表格行：「本期费用化研发投入  3,449」。
    #
    # ⚠ 上面那条要求 ≥3 格，这类只有两格，整条判据直接跳过——
    # 实测宝钢 130 条候选里有 10 条是这种，全是研发投入表的行标签加一个数。
    #
    # 判据要收得紧，因为**正文里也会出现两个空格**（PDF 排版如此）：
    # 「本年度实现营业收入  322,116 百万元。」与它只差一点。
    # 分开它们的是——正文的数值带单位，句子带句号；表格行两样都没有。
    if (
        len(cells) == 2
        and _NUMERIC_CELL_RE.match(cells[1])
        and not stripped.endswith(("。", "！", "？", "；", "："))
    ):
        return True

    # 连续的数字单元格 ≥ 3 个 → 表格行。
    #
    # ⚠ 为什么不能只靠上面那条「非数字格 ≤ 1」：**附注列会把说明文字
    # 直接拼在数字后面**，凭空多出一个非数字格，整条判据就失效了：
    #
    #     应收票据  627  0.2  29,190  8.7  -97.9  新金融工具准则列报项目不同所致…
    #     └ 7 个格，5 个是数字 —— 差 1 格没到 len-1，判成正文
    #
    # 而「数字占比 > 0.5」那条退路被同一段说明文字稀释掉了（实测 0.89 → 0.45），
    # 于是这样一行进了候选句，最后出现在会计要逐条判的 P 表里。
    # 实测 196 条候选里 12 条是这种（6%）。
    #
    # 数**连续段**两个问题都没有：正文里的数字被标点和词距隔开，
    # 不会用双空格排成三列以上。
    run = 0
    for cell in cells:
        if _NUMERIC_CELL_RE.match(cell):
            run += 1
            if run >= 3:
                return True
        else:
            run = 0

    # ---- 形态四：两列数据 + 一列说明（受限资产表）--------------------------
    #
    #    应收账款  36.6  通过保理业务作为质押物取得短期借款
    #
    # 上面那条「非数字格 ≤ 1」要求**只有一个**非数字格，这里有两个（标签 +
    # 说明），差 1 格没够上，整行进了正文。实测宝钢 353 条主张里 11 条是这种，
    # 全是「截至报告期末主要资产受限情况」那张表——而它偏偏被抽中，
    # 是因为行标签「应收票据 / 应收账款」正好是回款主题的触发词，
    # 别的行（货币资金、固定资产）没触发词，反而没进。
    #
    # 判据：恰好**一个**纯数字格，且它**不带单位**（表里的金额本来就不带，
    # 单位在表头上写着「单位：百万元」），其余格子里没有句末标点。
    # 「不带单位」这一条是收紧用的：正文里的数值几乎都带单位，
    # 表格里的数字才光秃秃地待在格子里。
    if _looks_like_note_row(cells):
        return True

    # ---- 形态五：两格，数字**紧贴在说明前**（列对齐在提取时丢了）----------
    #
    #    应收票据  524质押开票5.1亿元，贴现0.2亿元
    #    货币资金  1,003财务公司存放中央银行法定准备金存款
    #
    # 同样是受限资产表：这一年的 PDF 里数字列与说明列之间只剩一个空格，
    # `_MULTI_SPACE` 切不开，于是「524质押开票…」成一格，上面两条都判不出来。
    #
    # ⚠ 判据必须要求标签里**有汉字**：`3.  2016年公司经营计划并不构成…`
    #   是一句真的正文，它的第一格是「3.」——只有编号没有汉字。
    if _looks_like_label_glued_to_number(cells, stripped):
        return True

    # 退路：列对齐在提取时丢失、整行几乎全是数字字符的情况
    if len(stripped) >= 8:
        digits = sum(ch.isdigit() for ch in stripped)
        if digits / len(stripped) > 0.5:
            return True

    return False


#: 格子里出现这些就说明它是句子、不是单元格
_CELL_PUNCT = "。！？；："

#: 一个「光秃秃的数」：只有数字、千位分隔符和符号位，没有单位、没有标点
_BARE_NUMBER_RE = re.compile(r"^[-−—–]?\d[\d,]*(?:\.\d+)?$")

#: 行标签里至少要有这么多汉字才算标签。`3.`（只有编号）不算。
_LABEL_MIN_HAN = 2

_HAN_RE = re.compile(r"[一-鿿]")


def _looks_like_note_row(cells: list[str]) -> bool:
    """形态四：恰好一个无单位的纯数字格，其余格都不是句子。"""
    if len(cells) < 3:
        return False
    bare = [i for i, c in enumerate(cells) if _BARE_NUMBER_RE.match(c.strip())]
    if len(bare) != 1:
        return False
    return not any(
        ch in c for i, c in enumerate(cells) if i != bare[0] for ch in _CELL_PUNCT
    )


#: 数字**紧贴**着汉字：`524质押开票…`。列对齐保住的时候数字与下一列之间
#: 至少有空格（`524  质押开票…`），只有列被挤没了才会粘在一起。
_GLUED_NUMBER_RE = re.compile(r"^\d[\d,]*(?:\.\d+)?[一-鿿]")


def _looks_like_label_glued_to_number(cells: list[str], stripped: str) -> bool:
    """形态五：两格，第二格是一个**紧贴着汉字**的数字，第一格是个短标签。"""
    if len(cells) != 2:
        return False
    label, rest = cells
    if not _GLUED_NUMBER_RE.match(rest):
        return False
    # 标签要短、要没有标点、要有汉字
    if len(label) > 14 or any(ch in label for ch in _CELL_PUNCT + "，、（）()"):
        return False
    if len(_HAN_RE.findall(label)) < _LABEL_MIN_HAN:
        return False
    # 整行不许以句末标点收尾——正文才会那样收尾
    return not stripped.endswith(("。", "！", "？"))


def iter_sentences(text: str, *, min_length: int = 8) -> tuple[Sentence, ...]:
    """把一段正文切成候选句子。

    `min_length` 以下的不收——「其中：」「合计」这类碎片没有主张价值，
    但会污染主题词的命中率。
    """
    if not text:
        return ()

    joined = join_wrapped_lines(text)
    out: list[Sentence] = []
    cursor = 0
    for raw_line in joined.split("\n"):
        line = raw_line.strip()
        if not line:
            cursor += len(raw_line) + 1
            continue
        if looks_like_table_row(line) or HEADING_RE.match(line):
            cursor += len(raw_line) + 1
            continue

        base = cursor
        for piece in SENTENCE_END.split(line):
            piece = piece.strip()
            if len(piece) >= min_length:
                out.append(
                    Sentence(
                        text=piece,
                        index=len(out),
                        offset=base,
                        context=line,     # 整段，供口径判断用（见 Sentence.context）
                    )
                )
            base += len(piece)
        cursor += len(raw_line) + 1

    return tuple(out)


def numeric_tokens(text: str) -> tuple[str, ...]:
    """取一行里的数字 token。供主题规则判断「有没有具体数值」。"""
    return tuple(_NUMBER_RE.findall(text))
