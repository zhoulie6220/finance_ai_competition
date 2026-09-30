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


@dataclass(frozen=True)
class Sentence:
    """一段正文里切出的一个候选句子。"""

    text: str
    index: int          # 在所属 section 内的序号，进 claim_id 的哈希
    offset: int         # 在原始文本里的起始位置，用于回溯与高亮


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

    # 退路：列对齐在提取时丢失、整行几乎全是数字字符的情况
    if len(stripped) >= 8:
        digits = sum(ch.isdigit() for ch in stripped)
        if digits / len(stripped) > 0.5:
            return True

    return False


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
                    )
                )
            base += len(piece)
        cursor += len(raw_line) + 1

    return tuple(out)


def numeric_tokens(text: str) -> tuple[str, ...]:
    """取一行里的数字 token。供主题规则判断「有没有具体数值」。"""
    return tuple(_NUMBER_RE.findall(text))
