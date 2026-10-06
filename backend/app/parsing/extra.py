"""三张主表**之外**的那些规整表格。

`statements.py` 只认「合并资产负债表 / 合并利润表 / 合并现金流量表」三张主表，
而年报里还有几张同样规整、同样重要的表散在别处：

    近三年主要会计数据和财务指标      第二节，每股收益 / 扣非净利润 / 加权 ROE
    非经常性损益项目和金额            第二节，非经常性损益 + 计入当期损益的政府补助
    现金流量表补充资料                附注，固定资产折旧 / 无形资产摊销
    产销量情况                        第三节，钢材产量、销量、库存

不做这些表，字段字典里就有整片整片**永远填不上**的指标——页面上显示
「未在年报中定位到」，而真实情况是**那张表压根没人去读**。两者看起来一模一样。

## 为什么不并进 `statements.py`

主表有两条硬假设，这两张表一条都不满足：

1. **每一列都必须是年份**。主表的表头是 `项目 2024年度 2023年度`；
   而「近三年主要会计数据」的表头是

       2024年   2023年   本期比上年同期增减(%)   2022年

   中间那一列是**变动率，不是年度**。`_period_from_header` 遇到没有年份的列
   会拒绝整张表（这是对的——主表认不出期间就该拒绝），但用在这里，
   拒绝的理由跟真正的问题毫无关系。

2. **列是右对齐的数值列**，这一条两张表都满足，所以列聚类可以照用。

所以本模块**复用 `statements.py` 的列聚类与行读取**（同一套算法，不抄第二遍），
只把「表头认期间」这一段换成能容忍夹杂列、且能在一节里认**多个表头段**的版本。

## 一节里可能有多个表头段

宝钢 2024 的「近三年主要会计数据」一节里其实**有两张表**，各有各的表头：

    y=132.79  主要会计数据  2024年  2023年  同期增减(%)  2022年     ← 期间数
    y=162.07  营业收入      322,116  344,500  -6.50  367,778
    ...
    y=247.39  2024年末  2023年末  年同期末增  2022年末              ← 时点数
    y=275.47  归属于上市公司股东的净资产  200,548  200,325  0.11  194,623

只认「第一个表头」的话，下半张表的数字会挂到上半张表的列上——**数值全对、
期间全错**，而且不会报错。所以按「表头段」切，每段各自认列。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import pymupdf

from app.parsing import models as M
from app.parsing.reader import LogicalLine, read_page
from app.parsing.statements import (
    COLUMN_TOL_PT,
    _cluster_columns,
    _match_header_labels,
    _parse_rows,
    _value_cells,
)

#: 表头里的年份格。**必须是一个「只有年份」的格子**，后面最多跟这几个后缀。
#:
#: ⚠ 为什么要卡这么死，两个都是实测撞出来的：
#:
#:   1. 只判 `^20\d{2}` 的话，单位为百万元时某个恰好四位、又以 20 开头的
#:      **数值**（`2,024` 带千分位躲得过，裸写 `2024` 躲不过）会被当成表头格。
#:   2. 只判「以年份开头」的话，宝钢 2020 的 `2019年主要会计数据` 会被当成
#:      年份格——它是**表名**混在表头行里的那一格。多出来的这一格会配到
#:      「本期比上年同期增减」那一列上，于是整列年度右移一位，
#:      表现是**最后一个年度的数值变成了同比增减率**（−2.72 被当成 2018 年
#:      的营业收入），而两边都是数字，页面上看不出来。
#:
#: 「（调整前）」这类重述列**故意不收**——它是同一年的第二个口径，
#: 取哪一个是个口径问题（CLAUDE.md：重述值与原值双行并存、永不覆盖），
#: 解析层不替它做主。
_YEAR_CELL_RE = re.compile(r"^20\d{2}\s*年(末|度|金额|12月31日|12月31日金额)?$")

#: 单位行：`单位：百万元  币种：人民币`
_UNIT_RE = re.compile(r"单位[：:]\s*(元|千元|万元|百万元)")

#: 期间标签里的「末」——`2024年末` 说明这一列是**时点数**。
_INSTANT_RE = re.compile(r"年末|期末|期初|末$")

#: 「本期 / 上期」两列。现金流量表补充资料就是这种——**列标题不是年份**，
#: 年份要从年报年份推。三家用词不同：`本期金额/上期金额`（宝钢、首钢）、
#: `本期数/上年同期数`（华菱 2022/2023）、`本年金额/上年金额`（华菱 2024）。
_CURRENT_RE = re.compile(r"^(本期|本年|本期金额|本年金额|本期数|本年发生额|本期发生额)$")
_PRIOR_RE = re.compile(r"^(上期|上年|上期金额|上年金额|上年同期数|上期数|上年发生额|上期发生额)$")


@dataclass(frozen=True)
class ExtraRow:
    label: str
    #: 与 `ExtraTable.values` 的列一一对应；None = 该格为空或为 `-`。
    values: tuple[Decimal | None, ...]
    page_no: int
    source_text: str
    #: 这一行自己的单位。None = 用整张表的。
    #: `named_columns` 模式下同一张表里可以混着「元」和「%」两种列
    #: （产能表就是：产能是万吨、产能利用率是百分比），所以单位要能逐行不同。
    unit: str | None = None
    unit_factor: Decimal | None = None


@dataclass(frozen=True)
class ExtraTable:
    """一张抽取出来的附加表。"""

    key: str
    title: str
    page_no: int
    scope: str
    #: 落进 `fact_observation.source_location`。**位置本身就是可信度信息**：
    #: 三张主表 > 主要指标表 > 附注 > 正文叙述。
    location: str
    unit: str
    unit_factor: Decimal
    #: 与每一列的 `values` 一一对应。
    periods: tuple[str, ...]
    #: 每一列表头里的原文（`2024年末`）。**时点/期间由它判**，不靠表的种类猜。
    period_labels: tuple[str, ...]
    rows: tuple[ExtraRow, ...]
    period_source: str

    @property
    def columns(self) -> tuple[M.ColumnSpec, ...]:
        """给上层复用 `financial_fact.period_kind` 的判定用的最小信息。"""
        return tuple(
            M.ColumnSpec(
                raw_label=self.period_labels[i],
                period=self.periods[i],
                period_kind="instant" if _INSTANT_RE.search(self.period_labels[i])
                else "current",
                x_left=0.0, x_right=0.0,
            )
            for i in range(len(self.periods))
        )


@dataclass(frozen=True)
class CellRule:
    """`named_columns` 模式下的一条取值规则：**第几行的第几列** → 哪个字段。

    专项储备表就长这样：

        项目        期初余额        本期增加        本期减少        期末余额
        安全生产费   2,000.00      282,744.51      281,130.22      1,614.23

    行名一律叫「安全生产费」，**四个数说的是四件不同的事**。所以这一项
    **不能靠行名映射**——靠行名的话四条会同时命中「安全生产费」，
    把期初余额当成计提数落库，而两个都是金额，页面上看不出来。
    这也是 `safety_production_fee_increase` 的别名刻意留空的原因。
    """

    row_re: re.Pattern[str]
    col_re: re.Pattern[str]
    metric_key: str
    #: 相对年报年份的偏移。`本期增加` 是 0；`期初余额` 是上一年，应填 -1
    period_offset: int = 0
    period_kind: str = "current"
    #: 这一格自己的单位。None = 用本节 `单位：X` 那一行的。
    #: 产能利用率那种百分比列没有单位行，必须在这里自己说清楚。
    unit: str | None = None


@dataclass(frozen=True)
class TextRule:
    """从**正文句子**里取值。捕获组 1 是数值。

    只有一处用得上，但这一处非有不可：会计 2026-10-06 点名
    「华菱 2022 年可以采用明确披露的计提数 286,112,204.89 元」，
    而那句话在专项储备表**下面的正文**里，不是表格格子：

        公司本期计提安全生产费286,112,204.89 元，已全部使用完毕。

    这是**唯一**一份明写「计提」的年报（其余 15 份只有增减变动表）。
    不做这一条的话，`safety_production_fee_provision` 一个值都没有，
    而「有 1 条」和「一条都没有」在页面上分不出来。
    """

    text_re: re.Pattern[str]
    metric_key: str
    unit: str = "元"
    #: 期间相对年报年份的偏移。这句话说的就是本年度，所以是 0。
    period_offset: int = 0


@dataclass(frozen=True)
class SectionSpec:
    """一节附加表的定位规则。

    `title_re` / `stop_re` 都作用在**归一化后**的行文字上（去空白、去行首序号），
    所以 `七、近三年主要会计数据和财务指标` 与 `(一)主要会计数据` 都能写。
    """

    key: str
    title_re: re.Pattern[str]
    #: 遇到这一行就停。默认是「下一个大节」（`八、…`）。
    stop_re: re.Pattern[str] = re.compile(r"^[一二三四五六七八九十]+、")
    scope: str = M.SCOPE_CONSOLIDATED
    #: 落进 `fact_observation.source_location`。**位置本身就是可信度信息**：
    #: 三张主表 > 主要指标表 > 附注 > 正文叙述。同一笔数出现在两处时，
    #: 裁决要先看这个。
    location: str = "notes"
    #: 列的期间怎么来：
    #:
    #:   `header_year`（默认）  表头里印着年份（`2024年金额`），直接读。
    #:   `current_prior`        表头是「本期 / 上期」，**年份从年报年份推**。
    #:                          现金流量表补充资料是这种。
    #:   `named_columns`        表头是**列名**（期初余额/本期增加/…），
    #:                          由 `cells` 里的规则决定哪一行哪一列进哪个字段。
    #:                          专项储备、产能利用率表是这种。
    #:
    #: ⚠ `current_prior` 只收**本期**那一列。上期列印的是同一笔数的另一种来源
    #:   （2024 年报的「上期」= 2023 年报的「本期」），两列都收会得到重复事实，
    #:   而重复事实会互相「交叉验证通过」——**看起来还更可信**。
    mode: str = "header_year"
    #: 只有 `named_columns` 用。
    cells: tuple[CellRule, ...] = ()
    #: 从**正文句子**里取值的规则，和 `cells` 可以同时给。
    text_rules: tuple[TextRule, ...] = ()
    #: 行名 → 字典别名。**只在行名本身有歧义时才用**。
    #:
    #: 目前只有一处：非经常性损益表的最后一行印的是 `合计`，而
    #: 「合计」在一份年报里出现几十次，直接把它当别名会让**每一张表的合计行**
    #: 都映射到非经常性损益上。所以在这里显式把这一节里的 `合计`
    #: 说明成 `非经常性损益合计`——字典那边本来就有这个别名，
    #: 映射规则仍然只存在于字典里，不搬进解析层。
    alias_hint: dict[str, str] = field(default_factory=dict)
    #: 这一节里**明确不收**的行名（归一化后精确比较）。
    #:
    #: 曾经用来挡 `使用权资产折旧/摊销`——那时它是 `无形资产摊销` 的别名，
    #: 两行都收会让同一 (指标, 期间) 出现两条不相等的观测，
    #: 被 `resolve()` 记成一处**根本不存在**的重述差异。
    #: 2026-10-06 会计答复把两者拆成了独立字段
    #: （`right_of_use_asset_depreciation`），所以这个挡板撤了，
    #: 两个字段各自映射各自的行。**这个口子留着给以后真需要时用。**
    skip_labels: tuple[str, ...] = ()
    #: 最多往后读几页。附加表比主表短，超了说明 stop 没命中。
    max_pages: int = 3


def _flat(text: str) -> str:
    """只去空白。**判 stop 用它，不用 `_norm`。**

    ⚠ `_norm` 会把行首的 `八、` 剥掉，而 `stop_re` 要判的恰恰就是它。
    用 `_norm` 判的话 `^[一二三四五六七八九十]+、` **永远命中不了**，
    整节会一路读到页数上限，把后面几张表的数据一起吸进来——
    实测：非经常性损益表里混进了「采用公允价值计量的项目」的 8 行，
    而且每行都带着看似正常的数值。
    """
    return re.sub(r"\s+", "", text)


def _norm(text: str) -> str:
    """行文字归一化：去空白，剥行首的 `七、` / `(一)` / `1.`。"""
    s = _flat(text)
    s = re.sub(r"^[（(][一二三四五六七八九十\d]+[）)]", "", s)
    s = re.sub(r"^[一二三四五六七八九十]+、", "", s)
    s = re.sub(r"^\d+[、.．]", "", s)
    return s


def _year_cells(line: LogicalLine) -> list:
    return [c for c in line.cells() if _YEAR_CELL_RE.match(c.text.strip())]


def _overlaps(cell, col) -> bool:
    """表头格与数值列在 x 上是否**真的重叠**（带一个列宽的容差）。"""
    return (
        cell.x0 < col.right_max + COLUMN_TOL_PT
        and cell.x1 > col.span_left - COLUMN_TOL_PT
    )


def _assign_periods(header: LogicalLine, columns: list) -> list[str]:
    """把表头里的年份格配到数值列上。**要求两者在 x 上真的重叠。**

    ⚠ **不能用 `statements.py::_match_header_labels` 那套「按最近中心配」。**
    那套对主表是对的（主表的年份格比数值列宽，而且和数值列不对齐，
    只能按中心找），但对这里的表会**把年份配到错误的列上**：

        宝钢 2019 的「主要会计数据」有 5 个数值列——

            营业收入  291,594  305,081  304,779  -4.42  289,093
                      2019年   2018年   2018年   增减    2017年
                                        重述后   重述前

        而表头只有 3 个年份格。按最近中心配时，前三个列配完之后
        `2017年` 只剩第四列（增减那列）可配——于是**同比增减率 −4.42
        被当成了 2017 年的营业收入**。两个都是数字，页面上看不出来。

    「必须重叠」这条把第四列挡掉了：`2017年` 那一格整体落在第四列的
    **右边**，只与第五列重叠。

    多个列同时重叠时取中心最近的那个——重述表里同一年的两列会同时重叠，
    而真正的数值在**前**一列（重述后）。
    """
    labels = [""] * len(columns)
    for cell in header.cells():
        text = cell.text.strip()
        if not _YEAR_CELL_RE.match(text):
            continue
        hits = [i for i, col in enumerate(columns) if _overlaps(cell, col)]
        if not hits:
            continue
        best = min(
            hits,
            key=lambda i: abs(
                cell.cx - (columns[i].span_left + columns[i].right_max) / 2
            ),
        )
        if not labels[best]:
            labels[best] = text
    return labels


def _is_header_segment(line: LogicalLine, spec: SectionSpec) -> bool:
    """这一行是不是（某张子表的）表头。"""
    texts = [c.text.strip() for c in line.cells()]
    if spec.mode == "current_prior":
        return sum(1 for t in texts if _CURRENT_RE.match(t) or _PRIOR_RE.match(t)) >= 2
    if spec.mode == "named_columns":
        # 表头 = **一个数值格都没有**、有两个以上格子的那一行，而且其中至少一格
        # 命中了某条规则的列名。
        #
        # ⚠ 不能写成「≥2 格命中列名」。专项储备那一节只有**一条**规则
        #   （只看「本期增加」列），于是最多命中 1 格，判据恒为假——
        #   整节一行都抽不出来，表现是「这家公司没披露」，而**不报错**。
        return (
            len(texts) >= 2
            and not _value_cells(line)
            and any(any(c.col_re.match(t) for c in spec.cells) for t in texts)
        )
    return len(_year_cells(line)) >= 2


def _assign_named(header: LogicalLine, columns: list) -> list[str]:
    """把表头格按**重叠**配到数值列上，标签原样保留（不做年份解析）。

    与 `_assign_periods` 同一套重叠判据，只是不再要求「像年份」——
    这里的列名是 `本期增加` 这种词。
    """
    labels = [""] * len(columns)
    for cell in header.cells():
        text = cell.text.strip()
        if not text:
            continue
        hits = [i for i, col in enumerate(columns) if _overlaps(cell, col)]
        if not hits:
            continue
        best = min(
            hits,
            key=lambda i: abs(
                cell.cx - (columns[i].span_left + columns[i].right_max) / 2
            ),
        )
        if not labels[best]:
            labels[best] = text
    return labels


def _assign_relative(
    header: LogicalLine, columns: list, report_year: str | None
) -> list[str]:
    """把「本期 / 上期」配到列上，**只保留本期那一列**。

    ⚠ 上期列印的是同一笔数的另一个来源（2024 年报的「上期」就是 2023 年报的
    「本期」）。两列都收的话，同一笔事实现库里会有两条观测，
    而它们**当然一致**——交叉校验会因为「多数一致」而更加确信，
    页面上多出一格，谁都看不出那是同一条数据的两个副本。
    """
    labels = [""] * len(columns)
    for cell in header.cells():
        text = cell.text.strip()
        hits = [i for i, col in enumerate(columns) if _overlaps(cell, col)]
        if not hits:
            continue
        best = min(
            hits,
            key=lambda i: abs(
                cell.cx - (columns[i].span_left + columns[i].right_max) / 2
            ),
        )
        if not labels[best]:
            labels[best] = text
    if report_year is None:
        return [""] * len(columns)
    return [
        report_year if _CURRENT_RE.match(lab) else ""
        for lab in labels
    ]


def _doc_unit(doc: pymupdf.Document) -> str | None:
    """整份年报声明的记账单位。

    ⚠ 华菱的附注表**本页没有 `单位：元` 那一行**，单位写在财务报表开头：
    「财务附注中报表的单位为：元」。取不到就返回 None——
    **绝不默认成元**：单位错了数字会差 10 的幂，而页面上看着完全正常。
    """
    pat = re.compile(r"单位[为是][：:]?\s*(元|千元|万元|百万元)")
    for i in range(min(doc.page_count, 120)):
        m = pat.search(doc[i].get_text())
        if m:
            return m.group(1)
    return None


def read_section(
    doc: pymupdf.Document, spec: SectionSpec, report_year: str | None = None
) -> ExtraTable | None:
    """在整份年报里找到 `spec` 描述的那一节并抽出来。找不到返回 None。

    `report_year` 只有 `mode='current_prior'` 的节用到——那一节表头不印年份。
    """
    for page_no in range(doc.page_count):
        lines = read_page(doc[page_no])
        anchor = next(
            (i for i, ln in enumerate(lines)
             if spec.title_re.search(_norm(ln.full_text))),
            None,
        )
        if anchor is None:
            continue
        table = _parse_section(
            doc, page_no, lines[anchor], lines[anchor + 1:], spec, report_year
        )
        if table is not None:
            return table
    return None


#: 每页的 y 都是各自独立的坐标系（都从 60 排到 760）。平铺成一个列表再按 y
#: 比较，「第 90 页 y=100」会被当成「第 89 页 y=211 之前」。给每页加一个远大于
#: 页高的偏移，页内顺序不变，跨页顺序也变得有意义。（与 `statements.py::_parse_one`
#: 同一招，理由一样。）
_PAGE_Y_OFFSET = 10_000

#: 附加表的列下限。主表用 3（防某一行的孤立数字自成一列），附加表放低到 2——
#: 「近三年主要会计数据」里时点那半张表**只有两行**。
_EXTRA_MIN_ROWS = 2


def _collect(
    doc: pymupdf.Document,
    page_no: int,
    title_line: LogicalLine,
    head: list[LogicalLine],
    spec: SectionSpec,
) -> list[LogicalLine]:
    """从标题行往下收集，撞到 `stop_re` 或到页数上限就停。"""
    out: list[LogicalLine] = [title_line]
    for i in range(spec.max_pages):
        if page_no + i >= doc.page_count:
            break
        page = head if i == 0 else read_page(doc[page_no + i])
        stopped = False
        for ln in page:
            flat = _flat(ln.full_text)
            if flat and spec.stop_re.search(flat):
                stopped = True
                break
            out.append(type(ln)(y=ln.y + i * _PAGE_Y_OFFSET, chars=ln.chars))
        if stopped:
            break
    return out


def _parse_section(
    doc: pymupdf.Document,
    page_no: int,
    title_line: LogicalLine,
    head: list[LogicalLine],
    spec: SectionSpec,
    report_year: str | None = None,
) -> ExtraTable | None:
    """把这一节按「表头段」切开，每段各自认列。"""
    lines = _collect(doc, page_no, title_line, head, spec)

    unit = next(
        (m.group(1) for ln in lines if (m := _UNIT_RE.search(ln.full_text))), None
    )
    if unit is None:
        # 本页没有单位行 → 退回**年报级声明**（华菱的附注表就是这种）。
        # 仍然取不到就拒绝：单位错了数字差 10 的幂，而页面上看不出任何异常。
        unit = _doc_unit(doc)
    if unit not in M.UNIT_FACTORS and spec.mode != "named_columns":
        # `named_columns` 的每一格自带单位（见 `CellRule.unit`），
        # 整张表没有统一单位是正常的——产能表里产能是万吨、利用率是百分比。
        return None
    if spec.text_rules:
        正文 = _parse_text_rules(lines, spec, title_line, page_no, report_year)
        if 正文 is not None and spec.mode != "named_columns":
            return 正文

    if spec.mode == "named_columns":
        t = _parse_named_columns(lines, spec, title_line, page_no, report_year, unit)
        if t is not None and spec.text_rules:
            # 表格与正文两种来源合到一张表里返回，上层不必知道有几个来源。
            正文 = _parse_text_rules(lines, spec, title_line, page_no, report_year)
            if 正文 is not None:
                return _merge(t, 正文)
        return t

    # ⚠ **按表头切段，列只在段内认。**
    #   一整节里认一次列的话，后一张表的数值会参与前一张表的列聚类
    #   （宝钢 2024 的「主要会计数据」和「主要财务指标」列位相同，
    #   但再往下的「非经常性损益」有 4 列、位置又不同），
    #   认出来的列集合被撑大，列与年度的对应整体错位——
    #   表现为**数值都对、挂到了错误的年度上**，不报错。
    starts = [i for i, ln in enumerate(lines) if _is_header_segment(ln, spec)]
    if not starts:
        return None

    # ⚠ **先按「期间 → 值」收，最后再拼成对齐的元组。**
    #
    #   不能一边解析一边固定 periods：一节里的两个表头段**期间可以不一样**。
    #   宝钢 2019 的「主要会计数据」期间段认得出 2019/2018/2017 三年，
    #   而紧跟的时点段（重述后/重述前两列挤在一起）只认得出 2019/2017 两年。
    #   若拿最后一段的 periods 当整张表的列，前面那段的行就会**带着三个值
    #   挂到两个年度上**——数值全对、列数对不上，而没有任何地方会报错。
    rows: list[tuple[str, dict[str, Decimal], int, str]] = []
    period_label: dict[str, str] = {}

    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        header, body = lines[start], lines[start + 1:end]
        flat_header = _flat(header.full_text)
        value_cells = [c for ln in body for c in _value_cells(ln)]
        columns = _cluster_columns(value_cells, min_rows=_EXTRA_MIN_ROWS)
        if not columns:
            continue
        hdr = (
            _assign_relative(header, columns, report_year)
            if spec.mode == "current_prior"
            else _assign_periods(header, columns)
        )
        # ⚠ **没有年份的列丢掉，而不是让整张表失败。**
        #   这正是本模块与 `statements.py::_period_from_header` 的实质差别：
        #   主表认不出期间必须拒绝（整张表会挂错年度），而这里那一列本来就是
        #   「本期比上年同期增减(%)」——它不是数据，是**派生量**，
        #   而且系统自己会算同比，不需要从年报里抄。
        keep = [
            (ci, m.group(1))
            for ci, lab in enumerate(hdr)
            if (m := re.search(r"(20\d{2})", lab))
        ]
        if not keep:
            continue
        # ⚠ **自证一致**：表头文字里出现的年份，必须与认出来的年份列**完全一致**，
        #   而且认出来的年份之间不能重复。
        #
        #   两条都是「数值全对、挂到错的年度上」这类问题的征兆，而那种错
        #   **页面上完全看不出来**。它们专门挡的是「重述年份」——宝钢 2016
        #   把 2015 年印成「调整后 / 调整前」两列，表头在 PDF 里排成主副标题
        #   交错的几行，抽出来是 `调整后2015年调整前` 这样一个格子：
        #   年份不在格子开头，认不出是年份格，**却照样会被当成普通表头格
        #   配到某一列上**，于是列与年度的对应整体错位。
        #
        #   取调整后还是调整前是个口径问题（CLAUDE.md：重述值与原值双行并存、
        #   永不覆盖），解析层不该替它做主，所以宁可这一段不解析——
        #   页面上会显式报告出来，而不是给一张看着正常的错表。
        years_in_cells = [year for _, year in keep]
        if spec.mode == "current_prior":
            # 「本期 / 上期」里**有且只有一列**该留下。留下两列说明这一段的
            # 列没配对（多半是把上期那一列也认成了本期），宁可不解析。
            # 这里**不能**做下面那条「年份自证」——这一段表头压根没印年份。
            if len(keep) != 1:
                continue
        elif (len(set(years_in_cells)) != len(years_in_cells)
                or set(years_in_cells) != set(re.findall(r"20\d{2}", flat_header))):
            continue
        for ci, period in keep:
            period_label.setdefault(period, hdr[ci])

        for parsed in _parse_rows(body, columns, {}, page_no + 1):
            values = {
                period: parsed.values[ci]
                for (ci, period) in keep
                if ci < len(parsed.values) and parsed.values[ci] is not None
            }
            if not values:
                continue
            if _norm(parsed.label) in spec.skip_labels:
                continue
            label = spec.alias_hint.get(_norm(parsed.label), parsed.label)
            rows.append((label, values, parsed.page_no, parsed.source_text))

    if not rows:
        return None
    periods = sorted({p for _, v, _, _ in rows for p in v}, reverse=True)
    return ExtraTable(
        key=spec.key, title=title_line.display_text, page_no=page_no + 1,
        scope=spec.scope, location=spec.location,
        unit=unit, unit_factor=M.UNIT_FACTORS[unit],
        periods=tuple(periods),
        period_labels=tuple(period_label.get(p, p) for p in periods),
        rows=tuple(
            ExtraRow(
                label=label,
                values=tuple(values.get(p) for p in periods),
                page_no=pg, source_text=text,
            )
            for label, values, pg, text in rows
        ),
        period_source="表头年份",
    )


def _parse_named_columns(
    lines: list[LogicalLine],
    spec: SectionSpec,
    title_line: LogicalLine,
    page_no: int,
    report_year: str | None,
    unit: str | None,
) -> ExtraTable | None:
    """`named_columns` 模式：按 `spec.cells` 里的（行, 列）规则逐格取值。

    期间一律从**年报年份**推（`period_offset`），因为这类表根本不印年份——
    它印的是「期初 / 本期增加 / 本期减少 / 期末」。
    """
    if report_year is None:
        return None
    starts = [i for i, ln in enumerate(lines) if _is_header_segment(ln, spec)]
    if not starts:
        return None

    collected: list[tuple[str, dict[str, Decimal], int, str, str | None]] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        header, body = lines[start], lines[start + 1:end]
        # 这一节里只要有一条规则要百分比列，整节的取值就都放开百分号——
        # 产能表的「产能利用率」印的是 `97%`，不放开的话那一列一个数都读不出来，
        # 列也就聚不起来（列聚类要求至少 N 行贡献了值）。
        pc = any(r.unit == "%" for r in spec.cells)
        columns = _cluster_columns(
            [c for ln in body for c in _value_cells(ln, allow_percent=pc)],
            min_rows=_EXTRA_MIN_ROWS,
        )
        if not columns:
            continue
        labels = _assign_named(header, columns)
        for parsed in _parse_rows(body, columns, {}, page_no + 1, allow_percent=pc):
            row_label = _norm(parsed.label)
            for rule in spec.cells:
                if not rule.row_re.search(row_label):
                    continue
                for ci, col_label in enumerate(labels):
                    if not col_label or not rule.col_re.match(col_label):
                        continue
                    if ci >= len(parsed.values) or parsed.values[ci] is None:
                        continue
                    period = str(int(report_year) + rule.period_offset)
                    collected.append((
                        rule.metric_key,
                        {period: parsed.values[ci]},
                        parsed.page_no,
                        parsed.source_text,
                        rule.unit,
                    ))

    if not collected:
        return None
    periods = sorted({p for _, v, _, _, _ in collected for p in v}, reverse=True)
    return ExtraTable(
        key=spec.key, title=title_line.display_text, page_no=page_no + 1,
        scope=spec.scope, location=spec.location,
        unit=unit or "%",
        unit_factor=M.UNIT_FACTORS.get(unit, Decimal("1")),
        periods=tuple(periods), period_labels=tuple(periods),
        rows=tuple(
            ExtraRow(
                label=label, values=tuple(values.get(p) for p in periods),
                page_no=pg, source_text=text,
                # 每一格自带单位：`unit` 为空时说明这一列不是金额
                # （产能利用率那种），此时因子取 1，值按原样落库。
                unit=row_unit or unit or "%",
                unit_factor=M.UNIT_FACTORS.get(row_unit or unit or "%", Decimal("1")),
            )
            for label, values, pg, text, row_unit in collected
        ),
        period_source=f"由年报年份 {report_year} 推（该表不印年份）",
    )


def _parse_text_rules(
    lines: list[LogicalLine],
    spec: SectionSpec,
    title_line: LogicalLine,
    page_no: int,
    report_year: str | None,
) -> ExtraTable | None:
    """跑 `spec.text_rules`：从正文句子里取出数值。

    ⚠ 在 `_flat` 之后的文字上匹配——`read_page` 出来的 `display_text`
    会在单元格之间补空格，而这句话恰好会被数字切开
    （`本期计提安全生产费  286,112,204.89 元`），直接拿原串写死正则匹配不上。
    """
    if report_year is None or not spec.text_rules:
        return None
    out = []
    for ln in lines:
        flat = _flat(ln.full_text)
        for rule in spec.text_rules:
            m = rule.text_re.search(flat)
            if not m:
                continue
            try:
                value = Decimal(m.group(1).replace(",", ""))
            except InvalidOperation:
                continue
            period = str(int(report_year) + rule.period_offset)
            out.append(ExtraRow(
                label=rule.metric_key, values=(value,), page_no=page_no + 1,
                source_text=ln.display_text,
                unit=rule.unit, unit_factor=M.UNIT_FACTORS.get(rule.unit, Decimal("1")),
            ))
    if not out:
        return None
    return ExtraTable(
        key=spec.key, title=title_line.display_text, page_no=page_no + 1,
        scope=spec.scope, location=spec.location,
        unit=out[0].unit or "元", unit_factor=out[0].unit_factor or Decimal("1"),
        periods=(str(report_year),), period_labels=(str(report_year),),
        rows=tuple(out), period_source=f"由年报年份 {report_year} 推（正文句子没有期间）",
    )


def _merge(a: ExtraTable, b: ExtraTable) -> ExtraTable:
    """两张同源的表合成一张：期间取并集（倒序），行按顺序接起来。"""
    periods = tuple(sorted({*a.periods, *b.periods}, reverse=True))

    def align(t: ExtraTable) -> list[ExtraRow]:
        return [
            ExtraRow(
                label=r.label,
                values=tuple(
                    dict(zip(t.periods, r.values)).get(p) for p in periods
                ),
                page_no=r.page_no, source_text=r.source_text,
                unit=r.unit, unit_factor=r.unit_factor,
            )
            for r in t.rows
        ]

    return ExtraTable(
        key=a.key, title=a.title, page_no=a.page_no, scope=a.scope,
        location=a.location, unit=a.unit, unit_factor=a.unit_factor,
        periods=periods, period_labels=periods,
        rows=tuple(align(a) + align(b)),
        period_source=f"{a.period_source}；{b.period_source}",
    )


# ------------------------------------------------------------------ 已登记的节

#: 「主要会计数据和财务指标」。三个地方都要按**名字**匹配，不按序号：
#:
#:   · 序号各家不同（宝钢 `七、`、华菱/首钢 `六、`）
#:   · 宝钢写「近三年主要会计数据和财务指标」，华菱/首钢写「主要会计数据和财务指标」
#:
#: 所以正则里**不能带「近三年」**——带了就只认得出宝钢，另两家一节都抽不出来，
#: 而表现是「这家公司没有这些数据」，看起来像年报没披露。
SUMMARY = SectionSpec(
    key="summary",
    title_re=re.compile(r"主要会计数据和财务指标"),
    location="indicator_table",
)

#: 「非经常性损益项目和金额」。最后一行 `合计` 就是非经常性损益，
#: 由 `alias_hint` 说明；同一张表里还有「计入当期损益的政府补助」。
#:
#: ⚠ 华菱/首钢写的是「非经常性损益项目**及**金额」，宝钢写「**和**金额」。
#:   只写「和」的话另两家的抽不出来，同样表现成「年报没披露」。
NONRECURRING = SectionSpec(
    key="nonrecurring",
    title_re=re.compile(r"非经常性损益项目[及和]金额"),
    alias_hint={"合计": "非经常性损益合计"},
    max_pages=2,
)

#: 「现金流量表补充资料」。**不印年份**，只有「本期金额 / 上期金额」两列，
#: 年份由年报年份推（`mode='current_prior'`）。
#:
#: ⚠ 定位只能靠这一个标题。**同一份年报里这张表出现两次**——合并口径一次、
#:   母公司口径一次（宝钢两份差 80–100 页），母公司版的数字量级小三倍
#:   （宝钢 2024：母公司折旧 57.7 亿 vs 合并 185.6 亿），而两版的
#:   `无形资产摊销` 行名**一模一样**。取错了不会报错，只会让折旧摊销少一大截。
#:   所以这里只认**合并口径**所在的那一片：`附注五` / `附注七` 的合并报表注释区。
#:   `read_section` 从第 1 页往后扫、**取第一次成功的那一节**，而合并版在前、
#:   母公司版在后，这个顺序本身就保证了取到合并版。
#:
#: ⚠ 行名在宝钢 2022 起改过（`投资性房地产及固定资产折旧` /
#:   `固定资产折旧及投资性房地产折旧`，2024 又把两个词对调），
#:   在字段字典里补别名才能映射上——**只改解析不改字典，数字抽出来了也进不了库**。
CASHFLOW_SUPPLEMENT = SectionSpec(
    key="cashflow_supplement",
    title_re=re.compile(r"现金流量表补充资料"),
    # ⚠ 停的判据**只能认顿号**（`69、所有者权益变动表项目注释`）。
    #   一开始写的是「行首数字 + 顿号或句点」，结果这张表**自己的行号**
    #   长成 `1．将净利润调节为经营活动现金流量：`，于是第一节刚开头就停了——
    #   表头行被留在里面、一行数据都没读到，表现是「这一节没找到」，
    #   而真正的原因跟「找不到」毫无关系。
    stop_re=re.compile(r"^\d{1,3}\s*、"),
    mode="current_prior",
    # 这张表只有一页。放宽了就会把后面的 `(2) 取得子公司的现金净额`、
    # `(6) 不属于现金及现金等价物的货币资金` 一起吸进来——
    # 后者也印着「本期金额 / 上期金额」，会被当成同一张表的续表。
    max_pages=1,
)

#: 「专项储备」附注。16 份年报**全都有**这张表，行名一律「安全生产费」，
#: 列是「期初余额 / 本期增加 / 本期减少 / 期末余额」。
#:
#: ⚠ 只取**本期增加**那一列。会计 2026-10-06 答复原话：
#:   「『本期增加』是专项储备增减变动，不必然等于当期计提，可能还包含其他
#:     增加事项」——所以它单独记 `safety_production_fee_increase`，
#:     **不冒充** `safety_production_fee_provision`（当期计提）。
#:     全样本只有华菱 2022 写了「本期计提」，那一条在正文里，由
#:     `MDNA_SAFETY_PROVISION` 单独收。
#:
#: ⚠ 列的三种写法（宝钢/首钢 `本期增加`、华菱 2024 `本年增加`）都要认，
#:   只写一种的话另两家抽不出来，表现是「这家公司没披露」。
SPECIAL_RESERVE = SectionSpec(
    key="special_reserve",
    title_re=re.compile(r"专项储备"),
    stop_re=re.compile(r"^\d{1,3}\s*[、.．]"),
    mode="named_columns",
    max_pages=2,
    cells=(
        CellRule(
            row_re=re.compile(r"安全生产费"),
            col_re=re.compile(r"^(本期增加|本年增加)$"),
            metric_key="safety_production_fee_increase",
        ),
    ),
    # 会计 2026-10-06 点名的那一句，全样本只有华菱 2022 写了。
    text_rules=(
        TextRule(
            text_re=re.compile(r"计提安全生产费([\d,]*(?:\.\d+)?)元"),
            metric_key="safety_production_fee_provision",
        ),
    ),
)

#: 宝钢「产能及产能利用率」。**只有宝钢 2023、2024 两年有这张表。**
#:
#: 会计 2026-10-06 答复原话：「宝钢产能表中的『钢』只能按原文标签记录为
#: 『钢』，**不能直接改写成粗钢**」；「新增或使用字段
#: `reported_capacity_utilization`，记录年报直接披露的 97%」；
#: 「不能把它直接用于 A-5 要求的『产量÷有效产能』，因为 16 份年报均没有
#: 披露有效产能」。
#:
#: 所以这里**只取「钢」行的产能利用率**：产能、产量两列没有对应字段
#: （`crude_steel_capacity` / `crude_steel_output` 明确不能收），
#: 原样留在 `source_text` 里。铁、坯材两行同理。
CAPACITY_TABLE = SectionSpec(
    key="capacity_table",
    title_re=re.compile(r"产能及产能利用率如下"),
    stop_re=re.compile(r"^[（(]?[一二三四五六七八九十\d]+[）)]?[、.]?\s*[^\d]"),
    mode="named_columns",
    max_pages=1,
    cells=(
        CellRule(
            row_re=re.compile(r"^钢$"),
            col_re=re.compile(r"^产能利用率"),
            metric_key="reported_capacity_utilization",
            unit="%",
        ),
    ),
)

EXTRA_SECTIONS: tuple[SectionSpec, ...] = (
    SUMMARY, NONRECURRING, CASHFLOW_SUPPLEMENT,
    SPECIAL_RESERVE, CAPACITY_TABLE,
)
