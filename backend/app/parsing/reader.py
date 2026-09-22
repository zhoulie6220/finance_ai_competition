"""PDF → 字符 → 逻辑行 → 单元格。

**为什么降到字符级。** 最初按 `get_text("words")` 切，在宝钢上工作得很好，
到了华菱直接崩：那份 PDF 的文字之间没有空格，PyMuPDF 把整行并成**一个词**：

    货币资金11,056,280,655.955,615,975,850.16

一个词就没法按列切了。而字符始终是一字一个、各带坐标，所以从字符往上搭。

**行聚类带容差。** 同一行里，中文字和数字的字号常常不同，baseline 差 1.7pt
（实测「货币资金」y=231.71、「11,056,280,655.95」y=229.98）。用精确相等分组
会把数字甩到另一行去——**而且这不会报错**，只会让那一行的数值凭空消失。

**单元格按 x 间距切。** 数字内部的字符间距不到 5pt，而列与列之间至少几十 pt，
所以「间距 > 10pt 就是换格」是个很干净的判据。
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

#: 同一行的 y 容差（pt）。字号差异造成的 baseline 偏移约 1.7pt，取 3 留余量。
#: 行距约 14pt，所以 3pt 不会把相邻两行并起来。
SAME_LINE_TOL_PT = 3.0

#: 单元格之间的 x 间距下限（pt）。数字内部间距 < 5，列间 > 40。
CELL_GAP_PT = 10.0

#: 折行判定阈值（pt）。实测普通行距 14.2、折行间距 6.7。
#: 取 10 而不是 10.5：宁可把「间距略小的新行」判成折行（拼出一个长行名，
#: 映射不上，看得见），也不要漏判折行（半截行名映射到某字段上，**不报错**）。
WRAP_GAP_PT = 10.0

#: 页眉页脚剔除（pt）。页眉里有年份，不剔会被当成一行行名读进去。
Y_TOP_PT = 60.0
Y_BOTTOM_MARGIN_PT = 80.0


@dataclass(frozen=True)
class Char:
    c: str
    x0: float
    x1: float
    y0: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass(frozen=True)
class Cell:
    """一个单元格：x 上连续的一段字符。"""

    text: str
    x0: float
    x1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass(frozen=True)
class LogicalLine:
    """一个逻辑行：同一 y 上的字符，含折行的后续段。"""

    y: float
    chars: tuple[Char, ...]

    def cells(self, gap: float = CELL_GAP_PT) -> list[Cell]:
        """按 x 间距切成单元格。

        折行段的字符也在这里，所以跨行行名的两段会**各成一个单元格**
        （它们在 x 上也确实隔开了），由调用方按 x 区间过滤后拼接。
        """
        if not self.chars:
            return []
        ordered = sorted(self.chars, key=lambda c: c.x0)
        out: list[Cell] = []
        buf: list[Char] = [ordered[0]]
        for prev, cur in zip(ordered, ordered[1:]):
            if cur.x0 - prev.x1 > gap:
                out.append(_as_cell(buf))
                buf = [cur]
            else:
                buf.append(cur)
        out.append(_as_cell(buf))
        return out

    def text_in(self, x_left: float, x_right: float) -> str:
        """取 x 区间内的文字。顺序按 (折行段, 段内 x)，与阅读顺序一致。"""
        return "".join(c.c for c in self.chars if x_left <= c.cx < x_right).strip()

    @property
    def full_text(self) -> str:
        return "".join(c.c for c in self.chars).strip()

    @property
    def display_text(self) -> str:
        """**给人看**的整行文字：在单元格之间补上空格。

        `full_text` 是按坐标顺序把字符直接拼起来的，于是排版上分开的单元格
        在字符串里粘成一片：

            其中：营业收入51322,115,845,919.76344,500,428,314.62

        这个字符串就是 `financial_fact.source_text`，也就是证据面板里那句
        「年报原文」。而证据面板正是本系统最有说服力的地方——一句糊在一起的
        数字串会让人以为解析坏了，即使数值全对。

        补空格**不改变任何解析结果**：解析用的是坐标，不是这个字符串。
        所以这里另起一个属性，而不是去改 `full_text`——后者参与表名、表头、
        单位、年份的识别，动它是在拿解析正确性换排版。
        """
        out: list[str] = []
        prev: Char | None = None
        for cur in self.chars:
            # ⚠ 按 `chars` 的**原顺序**走，不要按 x0 重排。
            #   折行段的字符是接在后面的（见 `merge_wrapped`），而折行段的 x
            #   又回到左边——重排会把第二段的行名插进第一段的数字中间，
            #   拼出一个谁也没印过的句子。
            #   顺带：跨段处 x 是在回退，差值为负，本来也不会误插空格。
            if prev is not None and cur.x0 - prev.x1 > CELL_GAP_PT:
                out.append("  ")
            out.append(cur.c)
            prev = cur
        return "".join(out).strip()

    @property
    def x0(self) -> float:
        return min(c.x0 for c in self.chars) if self.chars else 0.0

    @property
    def x1(self) -> float:
        return max(c.x1 for c in self.chars) if self.chars else 0.0


def _as_cell(chars: list[Char]) -> Cell:
    text = "".join(c.c for c in chars)
    return Cell(text=text.strip(), x0=chars[0].x0, x1=chars[-1].x1)


def page_chars(page: pymupdf.Page, *, skip_header_footer: bool = True) -> list[Char]:
    """页面上所有可见字符（默认剔掉页眉页脚）。"""
    y_top = Y_TOP_PT if skip_header_footer else 0.0
    y_bot = page.rect.height - Y_BOTTOM_MARGIN_PT if skip_header_footer else page.rect.height
    out: list[Char] = []
    for blk in page.get_text("rawdict")["blocks"]:
        if blk.get("type") != 0:      # 0 = 文本块，1 = 图片
            continue
        for line in blk["lines"]:
            for span in line["spans"]:
                for ch in span["chars"]:
                    x0, y0, x1, _ = ch["bbox"]
                    if y_top < y0 < y_bot and ch["c"].strip():
                        out.append(Char(c=ch["c"], x0=x0, x1=x1, y0=y0))
    return out


def group_lines(chars: list[Char]) -> list[list[Char]]:
    """按 y 把字符聚成物理行（带容差）。"""
    if not chars:
        return []
    buckets: list[list[Char]] = []
    for c in sorted(chars, key=lambda c: (c.y0, c.x0)):
        if buckets and abs(c.y0 - buckets[-1][0].y0) <= SAME_LINE_TOL_PT:
            buckets[-1].append(c)
        else:
            buckets.append([c])
    return [sorted(b, key=lambda c: c.x0) for b in buckets]


def merge_wrapped(phys: list[list[Char]]) -> list[LogicalLine]:
    """把折行并进它所属的逻辑行。

    ⚠ 折行段的**字符**原样并进来，不是只并文字——数值就藏在折行段里。
    最初写成「只并文字」时，`投资收益` 那几行连同它们的数字一起静默消失，
    整行被当成「没有数值的分类标题」跳过。
    """
    out: list[LogicalLine] = []
    for line in phys:
        y = line[0].y0
        if out and (y - out[-1].y) < WRAP_GAP_PT:
            out[-1] = LogicalLine(y=out[-1].y, chars=out[-1].chars + tuple(line))
        else:
            out.append(LogicalLine(y=y, chars=tuple(line)))
    return out


def read_page(page: pymupdf.Page, *, skip_header_footer: bool = True) -> list[LogicalLine]:
    return merge_wrapped(group_lines(page_chars(page, skip_header_footer=skip_header_footer)))
