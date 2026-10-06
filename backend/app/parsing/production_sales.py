"""宝钢年报「产销量情况分析表」的**合计行**。

这张表在「第三节 管理层讨论与分析 → 五、报告期内主要经营情况 →
(一)主营业务分析」下面，标题写作 `(2) 产销量情况分析表`
（2017 写 `(2)产销量情况分析表`，其余年份写 `(2).产销量情况分析表`）。

    主要产品   单位   生产量   销售量   库存量   生产量比上年增减(%)  ...
    冷轧碳钢板卷  万吨   2,082   2,121      49            1.9      ...
    热轧碳钢板卷  万吨   2,124   2,097      24           -2.6      ...
    ...
    合计        万吨   5,141   5,159      92           -1.0      ...

**只要 `合计` 那一行**，不要各产品分项。年报表下注里写明
「销售量中包含销售给宝日汽车板的碳钢产品…万吨，不包含宝日汽车板冷轧碳钢板卷的
销量…万吨」——分项之间口径互相重叠又互相扣除，单看某一行数字对不上任何东西。

## 两种版式

2015–2018 和 2019–2024 的表头**不是同一张表**：

    2015–2018   单位单独占一行（`单位：万吨`），表头没有「单位」列
                主要产品 | 生产量 | 销售量 | 库存量 | 3 个同比列

    2019–2024   表头多一列「单位」，每一行自己写 `万吨`
                主要产品 | 单位 | 生产量 | 销售量 | 库存量 | 3 个同比列

取单位的方式因此也不同，**但两种都必须从表里读出 `万吨`，读不到就返回 None**
（旧版式走 `_unit_line`，新版式走 `_row_label_and_unit` 里的单位格）。

## ⚠ 单位错了差 10000 倍，而页面上看起来完全正常

`万吨` 和 `吨`、`万元` 之间差的是 10 的幂，而页面上只是多一个或少一个字。
所以本模块**没有任何默认单位**：单位行读不到、单位列读不到、或者读出来不是
质量单位（拿到 `元` / `百万元` 说明定位到的根本不是这张表），一律返回 `None`——
宁可让上层显示「未在年报中定位到」，也不要给一个数字量级错了 10000 倍的数。

## ⚠ 不要复用 `statements._cluster_columns`

那里面有一条「附注号列」判据：**整列都是没有千分位、没有小数点的小整数**就当附注
号丢掉。它是对着三张主表写的（宝钢 2019 资产负债表的 `1,2,3,…` 被认成了一列），
用在这张表上会把 **`库存量` 整列丢掉**——钢厂的库存量恰好就是几十到几百的裸整数：

    宝钢 2015 库存量：123 / 44 / 25 / 20 / 211      全是裸整数，全 < 10000
    宝钢 2024 库存量： 49 / 24 /  6 /  3 /   8 / 2 / 92

丢掉之后 `inventory` 永远是 `None`，而**不会报错**，页面上看起来像年报没披露库存量。
所以这里自己写聚类（`_data_columns`），算法一致，只是**不做**那一步价值判断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

import pymupdf

from app.parsing.extra import _EXTRA_MIN_ROWS, _assign_named, _flat, _norm
from app.parsing.reader import Cell, LogicalLine, read_page
from app.parsing.statements import (
    COLUMN_TOL_PT,
    Column,
    _column_of,
    _value_cells,
    parse_number,
)

#: 小节标题。**只匹配标题本身，不匹配前面的序号**——序号各年写法不同
#: （`(2)` / `(2).`），而且 2016 年起同一页里还有 `(1)主营业务分行业…`。
_TITLE_RE = re.compile(r"产销量情况分析表")

#: 表头行里必然出现的那一格。用**整格相等**判，不用包含：
#: 表头上方还有一行折行的列标题 `生产量比  销售量比  库存量比`，
#: 按包含判会把那一行也当成表头，而它里面**没有**量列的数值区间。
_HEADER_LABEL = "主要产品"

#: 要取的三列。**整格相等**，理由同上——`生产量比上` 含「生产量」，
#: 按包含判会把同比列的折行标题配到量列上，取出来的是增减率。
_QTY_LABELS: tuple[str, str, str] = ("生产量", "销售量", "库存量")

#: 合计行。**整格相等**（比较前去掉全部空白）：2020 年的「主营业务分产品」表
#: 把它印成 `合` `计` 两个格子，2020 年的产销量表是 `合计` 一格——
#: 两种都要认，所以归一化之后再比，而不是拿原串比。
_TOTAL_LABEL = "合计"

#: 质量单位。只认这四个。拿到 `元` / `百万元` 说明这一行根本不是产销量表的单位行
#: （同一页上方通常还有一张 `单位：百万元` 的收入成本表）。
_MASS_UNIT = r"吨|千吨|百万吨|万吨"

#: 旧版式的单位行：整行只有 `单位：万吨`。**整行匹配**，
#: 不能用 `search`——`单位：百万元  币种：人民币` 那种行会被 search 命中，
#: 于是产销量表拿到「百万元」，数字差 10 的幂而页面上看不出来。
_UNIT_LINE_RE = re.compile(rf"^单位[：:]({_MASS_UNIT})$")

#: 新版式的单位格：每一行的「单位」列里写 `万吨`。
_UNIT_CELL_RE = re.compile(rf"^({_MASS_UNIT})$")

#: 收集窗口。宝钢 2020 的表**跨页**：表头在 p15 底部，`合计` 行在 p16 顶部。
#: 只读本页的话合计行读不到，表现是「2020 年没披露产销量」——而它披露了。
#: 再多读就会读到下一张表。
_MAX_PAGES = 2


@dataclass(frozen=True)
class ProductionSalesRow:
    """`合计` 那一行。三个量的单位一律是 `ProductionSalesTable.unit`。"""

    #: 生产量。
    output: Decimal
    #: 销售量。
    sales: Decimal
    #: 库存量。印 `-`（本期无此项）或该格读不出数时为 None——**不当 0**。
    inventory: Decimal | None
    page_no: int
    #: `LogicalLine.display_text`，落进证据面板当「年报原文」。
    source_text: str


@dataclass(frozen=True)
class ProductionSalesTable:
    #: **表头所在页**（1-based）。跨页时它与 `rows[0].page_no` 不同，
    #: 上层据此能知道这张表是从哪一页开始读的。
    page_no: int
    #: 原样保留表上的单位（当前全部是 `万吨`）。**不换算**：值保持表上的原始数字。
    unit: str
    #: 只含 `合计` 那一行。找不到合计行时整个 `read_production_sales` 返回 None，
    #: 所以这个元组要么不出现、要么恰好一条。
    rows: tuple[ProductionSalesRow, ...]


def _data_columns(
    lines: list[LogicalLine], *, min_rows: int = _EXTRA_MIN_ROWS
) -> list[Column]:
    """按**右边界**把数值格聚成列（与 `statements._cluster_columns` 同一套算法）。

    数值右对齐，同列右边界实测只差 0.2pt，不同列差几十上百 pt，所以拿右边界聚类。
    用中心的话，`-0.5` 和 `2,215` 这种长短差很多的值会聚不到一起。

    ⚠ **与 `_cluster_columns` 的唯一差别是不丢「附注号列」。** 见模块 docstring：
    那条判据会把本表的 `库存量` 整列吃掉，而且不报错。
    """
    samples = [c for ln in lines for c in _value_cells(ln)]
    if not samples:
        return []
    by_right = sorted(samples, key=lambda c: c.x1)
    groups: list[list] = [[by_right[0]]]
    for c in by_right[1:]:
        if c.x1 - groups[-1][-1].x1 <= COLUMN_TOL_PT:
            groups[-1].append(c)
        else:
            groups.append([c])
    return [
        Column(
            right_min=min(x.x1 for x in g),
            right_max=max(x.x1 for x in g),
            span_left=min(x.x0 for x in g),
        )
        for g in groups
        if len(g) >= min_rows
    ]


def _header_line(lines: list[LogicalLine]) -> int | None:
    """表头行在 `lines` 里的下标。找不到返回 None。

    判据是「有一格恰好是 `主要产品`」。表头上方那行折行的列标题
    （`生产量比  销售量比  库存量比`）没有这一格，所以不会被认成表头。
    """
    for i, ln in enumerate(lines):
        if any(c.text.strip() == _HEADER_LABEL for c in ln.cells()):
            return i
    return None


def _unit_line(lines: list[LogicalLine]) -> str | None:
    """旧版式的单位行（`单位：万吨`）。取**表头之前最后一条**。

    取最后一条而不是第一条：同一页上方往往已经出现过 `单位：百万元`
    （主营业务收入成本表），它排在产销量表标题**之前**，所以本函数只收到
    标题之后的行；但仍然以最靠近表头的那一行为准，避免中间还夹着一张别的表。
    """
    for ln in reversed(lines):
        m = _UNIT_LINE_RE.match(_flat(ln.full_text))
        if m:
            return m.group(1)
    return None


def _row_label_and_unit(
    ln: LogicalLine, columns: list[Column], value_cells: list[Cell]
) -> tuple[str, str | None]:
    """一行的（行名，这一行自己的单位）。

    行名 = 第一个**属于某一列**的数值格左边全部文字，去掉其中的单位格。
    单位格就是新版式里那列 `万吨`。

    ⚠ **不直接用 `statements._parse_rows` 的 `label`**：它把「第一个数值格左边
    的全部文字」原样当行名，于是新版式的 `合计` 行读出来是 `合计万吨`
    （单位格也落在数值格左边），拿它跟 `合计` 比**永远不等**，
    整张表返回 None——表现是「这家公司没披露」，而真正的原因跟披露无关。
    """
    mapped = [c for c in value_cells if _column_of(c, columns) is not None]
    if not mapped:
        return "", None
    first_x = min(c.x0 for c in mapped)
    left = [c for c in ln.cells() if c.x1 <= first_x]
    unit = next((c.text.strip() for c in left if _UNIT_CELL_RE.match(c.text.strip())), None)
    label = _norm("".join(c.text for c in left if not _UNIT_CELL_RE.match(c.text.strip())))
    return label, unit


def _find_total(tail: list[LogicalLine]) -> int | None:
    """表体里 `合计` 行的下标（`tail` 从表头的下一行开始）。找不到返回 None。

    判据两条，**缺一不可**：行里有一格归一化后恰好是 `合计`，
    **而且这一行有数值格**。

    后一条不是装饰：同一页下方往往还有别的表也印 `合计`（出口分渠道表、
    成本分析表），它们同样在 `tail` 里。出口分渠道表整行是百分数，
    在这张表的列上一个数都读不出来——没有数值格，自然落选。

    ⚠ 把 `合计` 印成 `合` / `计` 两格的那种（宝钢 2020 的「主营业务分产品」表就是），
    归一化之后同样是 `合计`（`_norm` 去空白），与 `_TOTAL_LABEL` 的比较不受影响。
    """
    for i, ln in enumerate(tail):
        if not _value_cells(ln):
            continue
        if any(_norm(c.text) == _TOTAL_LABEL for c in ln.cells()):
            return i
    return None


def _parse(
    doc: pymupdf.Document, page_idx: int, after_title: list[LogicalLine]
) -> ProductionSalesTable | None:
    """在「标题所在页 + 下一页」的窗口里把合计行抽出来。抽不出返回 None。"""
    h = _header_line(after_title)
    if h is None:
        return None
    header = after_title[h]

    # 阅读顺序：表头所在页的表体 → 下一页。宝钢 2020 的 `合计` 行在下一页。
    #
    # ⚠ `pages` 与 `tail` **逐条对齐**，不能扫描时再统一填表头页的页码：
    #   跨页那张表的合计行实际印在第 16 页，「文件 → 页码」是证据链的终点，
    #   填成 15 会让用户点回上一页、看不见那个数字，而**不会报错**。
    tail: list[LogicalLine] = list(after_title[h + 1:])
    pages: list[int] = [page_idx + 1] * len(tail)
    for k in range(1, _MAX_PAGES):
        if page_idx + k < doc.page_count:
            more = read_page(doc[page_idx + k])
            tail.extend(more)
            pages.extend([page_idx + k + 1] * len(more))

    total = _find_total(tail)
    if total is None:
        return None
    # ⚠ **认列只用合计行之前的那几行**，也就是各产品分项行。
    #
    #   把 `tail` 整段（含合计行之后的注、说明、以及本页下方别的表）拿去聚类，
    #   会把列**链到一起**：单链聚类是在排序后的右边界之间传递的，
    #   下一张表某个数的右边界只要落在本表两列之间，两列就并成一组。
    #   并组之后 `labels` 的下标和 `values` 的键就错位了——实测 2015 的
    #   `库存量`（右边界 331.1）被隔壁表的一个 343.1 并走，
    #   读出来 `inventory=None`，而**不报错**，页面上像年报没披露库存量。
    body = tail[:total]
    columns = _data_columns(body)
    if not columns:
        return None
    labels = _assign_named(header, columns)
    qty = {lab: i for i, lab in enumerate(labels) if lab in _QTY_LABELS}
    if set(qty) != set(_QTY_LABELS):
        # 三列没有全部认出来 → 拒绝。少一列还硬出数的话，缺的那列会被
        # 悄悄当成 0 或 None 落到别的列上，而数字看起来很正常。
        return None

    #: 新版式的判据：表头里有一格恰好是 `单位`（旧版式没有这一列）。
    has_unit_column = any(c.text.strip() == "单位" for c in header.cells())
    unit_above = None if has_unit_column else _unit_line(after_title[:h])
    if not has_unit_column and unit_above is None:
        return None      # 旧版式但没有单位行 → 拒绝，绝不默认

    total_line = tail[total]
    values: dict[int, Decimal] = {}
    for c in _value_cells(total_line):
        i = _column_of(c, columns)
        if i is None:
            continue
        val, _is_dash = parse_number(c.text)
        if val is not None:
            values[i] = val

    output = values.get(qty["生产量"])
    sales = values.get(qty["销售量"])
    inventory = values.get(qty["库存量"])
    if output is None or sales is None:
        # 合计行里量列读不出数 → 拒绝。半张表比没有更危险：
        # 它会带着一个看着合理的数字进下游。
        return None
    label, row_unit = _row_label_and_unit(
        total_line, columns, _value_cells(total_line)
    )
    if label != _TOTAL_LABEL:
        return None

    if has_unit_column:
        if row_unit is None:
            return None      # 新版式但合计行没印单位 → 拒绝，不猜
        unit = row_unit
    else:
        unit = unit_above
    if unit is None or not _UNIT_CELL_RE.match(unit):
        return None

    return ProductionSalesTable(
        page_no=page_idx + 1,
        unit=unit,
        rows=(
            ProductionSalesRow(
                output=output,
                sales=sales,
                inventory=inventory,
                page_no=pages[total],
                # ⚠ 用 display_text 而不是 full_text：它会落进证据面板
                #   当「年报原文」。full_text 把单元格粘成一片
                #   （`合计万吨5,1415,15992-1.0-0.68.4`），数字全对但像解析坏了。
                source_text=total_line.display_text,
            ),
        ),
    )


def read_production_sales(
    doc: pymupdf.Document, report_year: str
) -> ProductionSalesTable | None:
    """从一份宝钢年报里抽出「产销量情况分析表」的合计行。找不到返回 None。

    `report_year` 这一版**不参与取值**：这张表只印本期数，没有需要推算的期间。
    参数留着是为了与 `extra.read_section` 的调用形状一致——调用方不必为这一张表
    单独写一个分支。（值取自表里那一行，年份由调用方按文件本身决定。）

    编号在 PDF 里是 1-based（`page_no = 页下标 + 1`），与
    `extra.ExtraTable.page_no` 一致；证据链上「文件 → 页码」要能对上纸面。

    ⚠ **逐页找、取第一次成功的那一份**，与 `read_section` 同一策略。
    `产销量情况分析表` 这个标题在整份年报里只出现一次（目录页不列到 (2) 这一层），
    但仍然按「找得到且解析得出」判定，防止某一页只是正文里提了一句表名。
    """
    for page_idx in range(doc.page_count):
        lines = read_page(doc[page_idx])
        anchor = next(
            (i for i, ln in enumerate(lines) if _TITLE_RE.search(_flat(ln.full_text))),
            None,
        )
        if anchor is None:
            continue
        table = _parse(doc, page_idx, lines[anchor + 1:])
        if table is not None:
            return table
    return None
