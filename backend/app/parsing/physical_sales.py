"""华菱 / 首钢年报「公司实物销售收入是否大于劳务收入」那张表的**销售量行**。

这张表在「第三节 管理层讨论与分析 → 二、主营业务分析 / 报告期内主要经营情况」
下面，标题写作 `(3)公司实物销售收入是否大于劳务收入`（首钢 2015 写 `(3)`，
华菱 2016 写 `（3）`——括号全半角两种都有）。版式三家一致：

    行业分类      项目      单位   2015年      2014年     同比增减
    销售量        吨        6,770,352  6,565,231    3.12%
    冶金          生产量    吨        6,750,016  6,567,209    2.78%
                  库存量    吨          206,204    226,540   -8.98%

**只要 `销售量` 那一行的本期值。** 生产量、库存量不收——它们在字段字典里
没有对应字段（会计 2026-10-06 答复明确「不自动映射」）。

## 为什么单独一个模块，不复用字段字典

这一行的行名是 `销售量`，而 `销售量` 这个别名在字典里**同时挂在三个指标上**：

    steel_sales_volume   钢材销量     aliases: 钢材销量, 销售量
    coal_sales_volume    商品煤销量   aliases: 商品煤销量, 煤炭销量, 销售量
    industry_sales_volume 行业聚合销量 aliases: 销售量

`dictionary.validate()` 会把它报成「同一别名挂在多个字段上」，而
`LabelIndex.from_db` 的处置是**冲突时留先来的那一个**（`metric_key` 行序靠前者），
于是 `销售量` 永远解析成 `steel_sales_volume`——**正是会计点名不许的那一个**：

> 华菱「钢铁行业」、首钢「冶金」这类行业聚合行的销量，年报没有说明是钢材
> 还是粗钢。**允许**用于「需求与产销」的方向判断与 Q3，但
> **不映射为通用 `steel_sales_volume`**、不参与其他任何数量计算。

所以这里**不走字典**，直接落 `industry_sales_volume`——与
`production_sales.py` 落 `steel_sales_volume` 是同一个做法：会计点过头的口径，
写成显式代码，不靠别名去猜。

> ⚠ 宝钢走的是另一张表（`产销量情况分析表`，按产品拆分、有 `合计` 行），
> 由 `production_sales.py` 抽，落 `steel_sales_volume`。
> **两家不是一回事**，别把这里的输出并过去。

## ⚠ 单位必须从这张表自己读出来

华菱 2020 及以前印 `吨`，2021 起改印 `万吨`——**同一家公司，同一张表，
换了个单位**。差 10000 倍，而页面上只是少一个字。

所以本模块**没有任何默认单位**：`单位` 那一格读不到、或者读出来不是质量单位
（拿到 `元` / `%` 说明定位到的根本不是这张表），一律返回 `None`。
宁可让上层显示「未在年报中定位到」，也不要给一个量级错了 10000 倍的数。

## 只取本期列

表里有「本期年 / 上期年」两列。上期那一列印的是**同一笔数的另一种来源**
（2024 年报的「上期」= 2023 年报的「本期」），两列都收会得到重复观测，
而重复观测会互相「交叉验证通过」——**看起来还更可信**。
与 `production_sales.py`、与 `extra.py` 的 `current_prior` 模式同一条规矩。

> 这张表**没有代码长期在抽它**。库里现有的 12 条（华菱 2016–2024、
> 首钢 2022–2024）是 2026-10-06 手工塞进 `financial_fact` 的，
> `extractor` 写着 `rule:industry-volume-v1`——**那个字符串在代码里根本不存在**，
> `fact_observation` 里 0 条。后果是它们和解析出来的事实长得一模一样
> （`validated`、`comparable=1`），而**没人能复现它们**。
> 本模块就是来把这个口子补上的：补完之后，这 19 条全部可再生。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

import pymupdf

from app.parsing.extra import _norm
from app.parsing.reader import read_page
from app.parsing.statements import parse_number

#: 小节标题。**只匹配标题本身**——前面的序号各年写法不同
#: （`(3)` / `（3）`），而且同一页上方还有 `(2)主营业务分行业…`。
_TITLE_RE = re.compile(r"实物销售收入是否大于劳务收入")

#: 表头里的年份格。与 `extra.py::_YEAR_CELL_RE` 同一条理由：卡死在
#: 「只有年份 + 可选后缀」，否则单位为吨时某个恰好四位、又以 20 开头的
#: 数值会被当成年份列。
_YEAR_RE = re.compile(r"^(20\d{2})\s*年$")

#: 要取的那一行。**整格相等**，不用包含——`钢材销量` 也含「销量」，
#: 而它属于另一张表、另一条口径。
_TARGET_LABELS = ("销售量", "销量")

#: 质量单位。只认这四个，拿到别的说明定位错了。
_MASS_UNIT = re.compile(r"^(吨|千吨|万吨|百万吨)$")

#: 单位 → 吨的换算因子。与 `parse_reports._TON_FACTORS` 同源，
#: **逐表读，绝不默认**。
_TON_FACTORS: dict[str, Decimal] = {
    "吨": Decimal("1"),
    "千吨": Decimal("1000"),
    "万吨": Decimal("10000"),
    "百万吨": Decimal("1000000"),
}

#: 表头行往下找几行就放弃。这张表紧跟在标题和「√是□否」后面。
_HEADER_LOOKAHEAD = 5
#: 数据行再往下找几行。表里只有三行（销售量 / 生产量 / 库存量）。
_ROW_LOOKAHEAD = 12


@dataclass(frozen=True)
class PhysicalSalesRow:
    """这张表里 `销售量` 行的本期值。"""

    #: 本期数值，**已按表里印的单位折算成吨**。
    tons: Decimal
    #: 表里原样印的单位（`吨` / `万吨`）。落库时留这个，便于复核。
    raw_unit: str
    #: 年报上这一行印的原始数值（未折算）。
    raw_value: Decimal
    #: 表头里本期那一列的年份。
    period: str
    page_no: int
    #: 落进证据面板的「年报原文」。用 `display_text` 而不是 `full_text`：
    #: 后者把单元格粘成一片（`销售量吨6,770,3526,565,2313.12%`），
    #: 数字全对但看起来像解析坏了。
    source_text: str
    #: 行业分类格（`冶金` / `钢铁行业`）。会计要求的「同一披露范围」靠它认。
    #: 有些年份这一格并在生产量行上，取不到就是 None。
    industry: str | None = None


def _header_years(line) -> list[tuple[str, object]]:
    """表头行里的年份格 → [(年份, 单元格)]，按 x 从左到右。"""
    out = []
    for cell in line.cells():
        m = _YEAR_RE.match((cell.text or "").strip())
        if m:
            out.append((m.group(1), cell))
    return sorted(out, key=lambda t: t[1].x0)


def read_physical_sales(
    doc: pymupdf.Document, report_year: str
) -> PhysicalSalesRow | None:
    """从一份**华菱 / 首钢**年报里抽出「销售量」行的本期值。找不到返回 None。

    逐页找、取第一次成功的那一份——与 `read_section` / `read_production_sales`
    同一策略。这张表的标题在整份年报里只出现一次，但仍然按「找得到且解析得出」
    判定，防止某一页只是正文里提了一句表名。

    `report_year` 这一版**不参与取值**：期间取自表头印的年份，不从文件名推。
    参数留着是为了与 `extra.read_section` 的调用形状一致。
    （真出现表头年份与文件名年份对不上时，以**表头**为准——那是年报自己印的。）
    """
    for page_idx in range(doc.page_count):
        lines = read_page(doc[page_idx])
        anchor = next(
            (
                i
                for i, ln in enumerate(lines)
                if _TITLE_RE.search(_norm(ln.full_text))
            ),
            None,
        )
        if anchor is None:
            continue
        row = _parse(lines, anchor, page_idx + 1)
        if row is not None:
            return row
    return None


def _parse(lines, anchor: int, page_no: int) -> PhysicalSalesRow | None:
    head = lines[anchor + 1: anchor + 1 + _HEADER_LOOKAHEAD]
    for line in head:
        years = _header_years(line)
        if len(years) != 2:
            # 表头必须**恰好两列年份**：本期 + 上期。多一列说明认到的
            # 不是这张表（或者表里混进了别的年份），少一列说明表头没读全。
            continue
        row = _find_sales_row(
            lines[anchor + 1: anchor + 1 + _ROW_LOOKAHEAD], years, page_no
        )
        if row is not None:
            return row
    return None


def _find_sales_row(lines, years, page_no: int) -> PhysicalSalesRow | None:
    current_year = years[0][0]

    for idx, line in enumerate(lines):
        row_cells = [(c, (c.text or "").strip()) for c in line.cells()]
        labels = [_norm(t) for _, t in row_cells]
        pos = next((j for j, t in enumerate(labels) if t in _TARGET_LABELS), None)
        if pos is None:
            continue

        unit = next(
            (t for _, t in row_cells[pos + 1:] if _MASS_UNIT.match(t)), None
        )
        if unit is None:
            # 认不出单位就**整表不收**。默认成「吨」的话数字差一万倍，
            # 而下游（判据回退、Q3）全都建立在它上面。
            return None

        values = []
        for _, text in row_cells[pos + 1:]:
            if _MASS_UNIT.match(text) or text.endswith("%"):
                continue
            value, _dash = parse_number(text)
            if value is not None:
                values.append(value)
        if len(values) != 2:
            # 本期、上期各一个数。数目对不上就拒绝——半张表比没有更危险，
            # 它会带着一个看着合理的数字进下游。
            return None

        raw_value = values[0]
        return PhysicalSalesRow(
            tons=(raw_value * _TON_FACTORS[unit]).quantize(Decimal("0.000001")),
            raw_unit=unit,
            raw_value=raw_value,
            period=current_year,
            page_no=page_no,
            source_text=line.display_text,
            industry=_industry_of(lines, idx),
        )
    return None


def _industry_of(lines, idx: int) -> str | None:
    """这一组行最左边那一格是不是行业分类（`冶金` / `钢铁行业`）。

    「行业分类」是**合并单元格**，年报只在组里第一行印一次，而且印在哪一行
    各家不同：首钢 2015 印在 `生产量` 那一行（`销售量` 行上没有），
    2022 又并到了 `生产量` 行的数值后面。所以这里只在**能确定**时返回值：

    · `销售量` 行自己带行业格（标签不在第一格）→ 取本行第一格；
    · 否则看下一行的第一格，它必须是「不是量名、不是单位、不是数字」的短文本。

    认不出就返回 `None`——这一格是给「同一披露范围」留的痕迹，
    猜错了不会报错，只会让两条不同口径的数据看起来同源。
    """
    def _first_text(j: int) -> str | None:
        if not (0 <= j < len(lines)):
            return None
        row = [c for c in lines[j].cells() if (c.text or "").strip()]
        return row[0].text.strip() if row else None

    def _looks_like_industry(text: str | None) -> bool:
        if not text or len(text) > 8:
            return False
        if _norm(text) in _TARGET_LABELS or text in ("生产量", "库存量"):
            return False
        if _MASS_UNIT.match(text) or text.endswith("%"):
            return False
        value, _dash = parse_number(text)
        return value is None

    row = [c for c in lines[idx].cells() if (c.text or "").strip()]
    labels = [_norm((c.text or "")) for c in row]
    pos = next((j for j, t in enumerate(labels) if t in _TARGET_LABELS), None)
    if pos is None:
        return None
    if pos > 0 and _looks_like_industry(row[0].text.strip()):
        return row[0].text.strip()
    nxt = _first_text(idx + 1)
    return nxt if _looks_like_industry(nxt) else None
