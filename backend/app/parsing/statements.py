"""定位三张主表、认出列、按列读出每一行。

## 列是**从数据里认出来的**，不是从表头推的

最初按表头那一行的词位置推列边界，在宝钢上工作得很好，换一家就崩——
华菱的表头是 `项目期末余额期初余额`（**没有年份**），而且整行文字连在一起
根本没有可用的词间距。

改用数据自身的性质：**报表里的数值是右对齐的**，所以同一列的数值
右边界重合得非常好（实测同一列里 `-` 的 x1=426.1、19 位长数字的 x1=425.9，
差 0.2pt），而不同列的右边界差上百 pt。按右边界聚类，列就自己浮出来了。

顺带解决了「空单元格」的问题：某行的第一列没印东西时，那个 `-` 靠**坐标**
落在第二列，而不是靠数序号被当成第一列。

## 期间

宝钢的表头带年份（`2024 年度`），华菱/首钢只写 `期末余额 / 期初余额`，
年份在表头上方的日期行里（`2024年12月31日`）。两种都要认，认不出就
**拒绝解析这张表**，而不是猜一个年份——猜错了整张表的数字都会挂到错误的
年度上，而且不会有任何提示。

## 母公司报表

`母公司资产负债表` 的字符串**包含** `资产负债表`。用 `in` 判断的话，
母公司报表会被当成合并报表读进来，而两者的数字完全不是一回事。
表名一律**归一化后精确相等**匹配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

import pymupdf

from app.parsing import models as M
from app.parsing.reader import Cell, LogicalLine, read_page

#: 一张表最多跨几页。超了就不往下读——不设上限的话，读到下一张表里去。
MAX_SPAN_PAGES = 6

#: 列的右边界聚类容差（pt）。同列内实测差 0.2pt，不同列差上百 pt。
COLUMN_TOL_PT = 15.0

#: 认列时，至少要有这么多行贡献了值，才承认它是一个列。
#: 防止某一行的一个孤立数字把「列」认出来。
MIN_ROWS_PER_COLUMN = 3

_NUMBER_RE = re.compile(r"^[（(]?[-－—–]?[\d,，]+(\.\d+)?[)）]?$")
_DASHES = {"-", "－", "—", "–", "―"}
_UNIT_RE = re.compile(r"单位[：:]\s*(元|千元|万元|百万元)")
_YEAR_RE = re.compile(r"(20\d{2})\s*年")
#: 表名前面的序号：'1、合并资产负债表' / '2. 合并利润表'
_TITLE_PREFIX_RE = re.compile(r"^\s*[0-9]{1,2}\s*[、.．]\s*")


def parse_number(text: str) -> tuple[Decimal | None, bool]:
    """单元格文本 → 数字。

    返回 `(值, 是否破折号)`：

        322,115,845,919.76   千分位
        -70,487,849.26       负号（可能是全角的 －）
        -                    明确披露为「无此项」

    **破折号不当 0。** 它是「本期无金额」，0 是「本期金额为零」——两者在
    比率里做分母的意义完全不同。上层同样不落库，但解析报告里要分开统计，
    混在一起就没法判断解析对不对。
    """
    t = text.strip().replace(" ", "")
    if not t:
        return None, False
    if t in _DASHES:
        return None, True
    if not _NUMBER_RE.match(t):
        return None, False
    negative = t.startswith("(") or t.startswith("（")
    body = t.strip("()（）").replace(",", "").replace("，", "")
    # ⚠ 只把**全角**负号换成 ASCII 的，**不能用 lstrip("-－—–")**。
    #   用 lstrip 的话 ASCII 的 `-70,487,849.26` 会被剥成正数——
    #   资产减值损失、公允价值变动损失这类科目全都会变成正数，
    #   而且**没有任何地方会报错**，只是利润表上多出来一大笔不存在的收益。
    #   实测宝钢 2024 的「信用减值损失 -70,487,849.26」就是这么被翻正的。
    if body[:1] in "－—–":
        body = "-" + body[1:]
    elif negative and not body.startswith("-"):
        body = "-" + body       # 中文报表用括号表示负数
    try:
        return Decimal(body), False
    except InvalidOperation:
        return None, False


def normalize_title(text: str) -> str:
    """表名归一化：去序号、去空白。"""
    return _TITLE_PREFIX_RE.sub("", text.strip()).replace(" ", "")


# ------------------------------------------------------------------ 列


#: 附注号：`51` / `(五)52` / `（六）1` / `附注五52`。
#:
#: ⚠ 附注号**不都是纯数字**。宝钢 2024 写 `51`，宝钢 2015 写 `(五)52`——
#: 后者不是数字，于是它既不算数值格（不会被当成值列），又落在行名区间里，
#: 把行名粘成 `其中：营业收入(五)52`。那一行整年映射不上，
#: 而表现是「2015、2016 两年没有营业收入数据」——看起来像那年没披露。
NOTE_CELL_RE = re.compile(
    r"^(?:附注|注)?[一二三四五六七八九十]*[（(]?[一二三四五六七八九十]{0,3}[）)]?\s*\d{1,3}$"
)


def _is_note_cell(cell: Cell) -> bool:
    t = cell.text.strip().replace(" ", "")
    if not t:
        return False
    _, is_dash = parse_number(t)
    if is_dash:
        return False
    return bool(NOTE_CELL_RE.match(t))


def _value_cells(line: LogicalLine) -> list[Cell]:
    """这一行里「长得像数值」的单元格。"""
    out = []
    for cell in line.cells():
        if not cell.text:
            continue
        val, is_dash = parse_number(cell.text)
        if val is not None or is_dash:
            out.append(cell)
    return out


@dataclass(frozen=True)
class Column:
    """数据里认出来的一列。

    `right_*` 用来判定**数值**属于哪一列（数值右对齐，右边界聚得极紧）；
    `span_left` 用来抓**表头文字**（表头跟数值不对齐，得按区间重叠找）。
    """

    right_min: float
    right_max: float
    span_left: float


def _cluster_columns(samples: list[Cell]) -> list[Column]:
    """按右边界把数值单元格聚成列。

    用右边界而不是中心：数值右对齐，长数字和 `-` 的中心能差 45pt，
    右边界只差 0.2pt（实测同一列 367.13 / 367.25 / 367.26）。
    """
    if not samples:
        return []
    by_right = sorted(samples, key=lambda c: c.x1)
    groups: list[list[Cell]] = [[by_right[0]]]
    for c in by_right[1:]:
        if c.x1 - groups[-1][-1].x1 <= COLUMN_TOL_PT:
            groups[-1].append(c)
        else:
            groups.append([c])
    # 先按「贡献行数够多」筛，再判附注列。
    # ⚠ 两步分开做时不要把 columns 和 groups 各自过滤一遍再 zip——
    #   两次过滤的条件不同，zip 出来的列和格子就错位了，而且是静默错位。
    sized = [
        (Column(
            right_min=min(x.x1 for x in g),
            right_max=max(x.x1 for x in g),
            span_left=min(x.x0 for x in g),
        ), g)
        for g in groups if len(g) >= MIN_ROWS_PER_COLUMN
    ]
    kept = [(c, g) for c, g in sized if not _looks_like_note_column(g)]
    # 只在还剩下别的列时才丢附注列：整张表都被判成附注列说明判据在这份年报上
    # 不成立，那时宁可原样保留（会读出错的列，看得见），也好过悄悄清空整张表。
    return [c for c, _ in (kept or sized)]


#: 附注号的量级上限。中文年报的附注号不会超过三位数，而报表数值（单位是元）
#: 几乎不可能小到一万以下。
NOTE_MAX = 10000


def _looks_like_note_column(cells: list[Cell]) -> bool:
    """这一列是不是「附注号」列。

    ⚠ **附注号也是右对齐的数字，会被聚成一个「列」。** 宝钢 2019 的资产负债表
    就是这样：认出了 3 列，第一列是 `1,2,3,…`，于是「货币资金」读出来是 `1`。
    数字看着像数字，位置看着像一列，**而且不会报错**——只有当有人去核对
    货币资金为什么只有 1 块钱时才会发现。

    判据：整列都是**没有千分位、没有小数点的小整数**。
    报表数值即使恰好很小，也几乎不可能整列都写成不带分隔符的裸整数
    （单位为元时，数值动辄上亿）。
    """
    if not cells:
        return False
    for cell in cells:
        t = cell.text.strip()
        if any(ch in t for ch in ",.，。") or not t.isdigit():
            return False
        if int(t) >= NOTE_MAX:
            return False
    return True


def _drop_note_columns(
    columns: list[Column], groups: list[list[Cell]]
) -> list[Column]:
    """丢掉附注号列。

    只在**还剩下别的列**时才丢：整张表都被判成附注列，说明这个判据在这份
    年报上不成立，那时宁可原样保留让人看见，也好过把整张表悄悄清空。
    """
    kept_cols: list[Column] = []
    for col, group in zip(columns, groups):
        if len(group) >= MIN_ROWS_PER_COLUMN and _looks_like_note_column(group):
            continue
        kept_cols.append(col)
    return kept_cols if kept_cols else columns


def _column_of(cell: Cell, columns: list[Column]) -> int | None:
    """这个**数值**单元格属于哪一列。"""
    for i, col in enumerate(columns):
        if col.right_min - COLUMN_TOL_PT <= cell.x1 <= col.right_max + COLUMN_TOL_PT:
            return i
    return None


#: 表头里属于「行名列」的词，不参与列的匹配。
_HEADER_NON_COLUMN = ("项目", "附注", "注释")


def _header_cells(header: LogicalLine) -> list[Cell]:
    """把表头行按**它自己的间距**切成单元格。

    `项目 附注五 2024年度 2023年度` → 四个格子；
    华菱的 `项目2022年12月31日2022年1月1日` → 也是四个。
    """
    return [c for c in header.cells() if c.text]


def _match_header_labels(header: LogicalLine, columns: list[Column]) -> list[str]:
    """把表头单元格配到各列上。

    ⚠ **按最近的列中心配，不能按「x 落在列区间内」取。**

    表头文字比数值列宽，而且和数值不对齐：实测华菱 2022 的
    `2022年12月31日` 跨 121pt，而它那一列的数值只占 68pt，还整体偏右。
    按区间取的话只能捞到尾巴上的「1日」——表现为「认不出期间」，
    而真正的原因跟期间毫无关系。

    表头格子也**不是**按序号一一对应的：`项目` 和 `附注五` 占了前两个格子，
    但它们不对应任何数值列。按最近中心配，它们自然被排除——
    离任何一列都远。
    """
    cells = [c for c in _header_cells(header)
             if not any(k in c.text for k in _HEADER_NON_COLUMN)]
    labels = [""] * len(columns)
    last = -1
    for i, col in enumerate(columns):
        anchor = (col.span_left + col.right_max) / 2
        best, best_d, best_j = "", float("inf"), -1
        # ⚠ 只能往右找没被用过的格子。两列各自独立取「最近的格子」的话，
        #   同一个格子会被两列同时认领——宝钢 2019 就是这样得到
        #   `['2019','2019','2018']` 的：两列都拿到了「2019年12月31日」，
        #   于是整列数字挂到了错误的年度上。
        for j, cell in enumerate(cells):
            if j <= last:
                continue
            d = abs(cell.cx - anchor)
            if d < best_d:
                best, best_d, best_j = cell.text, d, j
        if best_j >= 0:
            last = best_j
        labels[i] = best
    return labels


# ------------------------------------------------------------------ 表头


def _period_from_header(
    header: LogicalLine,
    columns: list[Column],
    doc_year: str | None,
    fallback_year: str | None = None,
) -> tuple[list[str], list[str | None], str]:
    """从表头行取每一列的表头文字与期间。

    返回 `(表头文字, 期间列表, 期间来源说明)`。取不到年份时用 `doc_year`
    （表头上方日期行里的年份）推：第一列是当年，第二列是上一年。

    **推不出来就返回 None，让调用方拒绝解析这张表。** 猜一个年份的代价是
    整张表的数字挂到错误的年度上，而且不会有任何提示。
    """
    labels = _match_header_labels(header, columns)

    periods: list[str | None] = []
    from_header = True
    for lab in labels:
        m = _YEAR_RE.search(lab)
        if m:
            periods.append(m.group(1))
        else:
            from_header = False
            periods.append(None)

    source = "表头年份"
    if not from_header:
        # 表头只写「期末余额 / 期初余额」，年份在表头上方的日期行里
        # （`2024年12月31日`）。首钢 2024 有几张表连日期行都没有，才退到文件名。
        year = doc_year or fallback_year
        if year is None:
            return labels, [None] * len(columns), "取不到年份"
        periods = [str(int(year) - i) for i in range(len(columns))]
        source = (
            f"由报表日期 {doc_year} 推" if doc_year
            else f"由文件名年份 {year} 推（表头与报表日期都没有年份）"
        )
    return labels, periods, source


def _doc_year(lines: list[LogicalLine], header: LogicalLine) -> str | None:
    """表头上方最近的那个年份 —— 表头只写「期末余额」时，期间只能从这里来。

    取**离表头最近**的一个。不能取最远的：报表页上方常有审计报告的落款
    （`中国北京二○二五年四月十六日`）或者上一张表的表头年份，那些都不是
    本表的期间。
    """
    above = [ln for ln in lines if ln.y < header.y]
    for ln in reversed(above):
        m = _YEAR_RE.search(ln.full_text)
        if m:
            return m.group(1)
    return None


# ------------------------------------------------------------------ 主体


def _title_lines(doc: pymupdf.Document) -> list[tuple[int, str, str, str]]:
    """找出所有「报表标题页」。

    一项里出现两个以上不同表名的，一定是目录 / 索引页，整页丢掉。
    这在宝钢 2015 上实测到了——第 76 页同时列出三张表的名字，
    不排除的话会被当成报表页，读出一堆「页码 + 标题」的假数据。
    """
    out: list[tuple[int, str, str, str]] = []
    for i in range(doc.page_count):
        titles: list[str] = []
        for ln in read_page(doc[i]):
            norm = normalize_title(ln.full_text)
            if norm in M.STATEMENT_TITLES:
                titles.append(norm)
        if len(titles) == 1:
            kind, scope = M.STATEMENT_TITLES[titles[0]]
            out.append((i, titles[0], kind, scope))
        elif len(titles) > 1:
            continue
    return out


def _parse_one(
    doc: pymupdf.Document, page_no: int, title: str, kind: str, scope: str,
    fallback_year: str | None = None,
) -> tuple[M.ParsedStatement | None, str]:
    """解析一张表。返回 (结果, 失败原因)。

    ⚠ **以标题行为锚**，不能拿整页当这张表。

    一张报表页的**开头往往是上一张表的续表尾**——宝钢 2023 的合并利润表那页，
    页首还有母公司资产负债表的最后几行（`减：库存股…`，带着两个数值）。
    拿整页去找表头的话，「第一行数据」会落在上一张表里，于是「表头上方
    找不到含『项目』的行」——报的是表头的问题，实际原因跟表头毫无关系。

    续页同理：只在遇到下一个表名之前的部分属于本表。
    """
    first = read_page(doc[page_no])
    title_y = next(
        (ln.y for ln in first if normalize_title(ln.full_text) == title), None
    )
    if title_y is None:
        return None, "找不到标题行"

    raw_pages: list[list[LogicalLine]] = [
        [ln for ln in first if ln.y > title_y]
    ]
    for nxt in range(page_no + 1, min(page_no + MAX_SPAN_PAGES, doc.page_count)):
        page_lines = read_page(doc[nxt])
        stop = next(
            (ln.y for ln in page_lines
             if normalize_title(ln.full_text) in M.STATEMENT_TITLES),
            None,
        )
        raw_pages.append([ln for ln in page_lines if stop is None or ln.y < stop])
        if stop is not None:
            break

    # ⚠ 每页的 y 是**各自独立**的坐标系，都是从 60 排到 760。
    #   直接平铺成一个列表再按 y 比较，「第 90 页 y=100 的行」会被当成
    #   「第 89 页 y=211 那行之前」，于是表头被认到了**下一页**的重复表头上，
    #   而下一页表头上方没有日期行 —— 报出来的是「取不到年份」，
    #   真正的原因跟年份毫无关系。
    #   给每页加一个远大于页高的偏移，页内顺序不变，跨页顺序也变得有意义。
    PAGE_Y_OFFSET = 10_000
    lines = [
        replace(ln, y=ln.y + i * PAGE_Y_OFFSET)
        for i, pg in enumerate(raw_pages)
        for ln in pg
    ]
    page_index = {id(ln): i for i, pg in enumerate(raw_pages) for ln in pg}

    unit = next(
        (m.group(1) for ln in lines if (m := _UNIT_RE.search(ln.full_text))), None
    )
    if unit not in M.UNIT_FACTORS:
        return None, f"读不到单位（得到 {unit!r}）"

    # 表头 = 第一行数据**上方**、最后一个含「项目」的行。
    #
    # ⚠ 不能要求以「项目」开头：宝钢 2019 的表头是
    #   `附注项目2019年12月31日2018年12月31日`，开头是「附注」。
    #   也不能只取第一个含「项目」的行——正文里提「项目」的地方太多了。
    data_rows = [ln for ln in lines if len(_value_cells(ln)) >= 2]
    if not data_rows:
        return None, "一行数据都没认出来"
    first_data_y = data_rows[0].y
    header = next(
        (ln for ln in reversed(lines)
         if ln.y < first_data_y and "项目" in ln.full_text.replace(" ", "")),
        None,
    )
    if header is None:
        return None, "读不到表头（数据行上方找不到含「项目」的行）"

    body = [ln for ln in lines if ln.y > header.y]
    # 拿表头**下面**的数据去认列：表头自己那行的数字是年份，会把列认歪
    columns = _cluster_columns([c for ln in body for c in _value_cells(ln)])
    if not columns:
        return None, "一行数值都没读出来"

    labels, periods, source = _period_from_header(
        header, columns, _doc_year(lines, header), fallback_year
    )
    if any(p is None for p in periods):
        return None, f"认不出期间：{source}"

    rows = _parse_rows(body, columns, page_index, page_no)
    if not rows:
        return None, "认出了列但一行数据都没读出来"

    cols = [
        M.ColumnSpec(
            raw_label=labels[i] if i < len(labels) else "",
            period=periods[i],
            period_kind="instant" if kind == M.BALANCE else "current",
            x_left=col.span_left - COLUMN_TOL_PT,
            x_right=col.right_max + COLUMN_TOL_PT,
        )
        for i, col in enumerate(columns)
    ]
    return M.ParsedStatement(
        kind=kind, scope=scope, unit=unit, unit_factor=M.UNIT_FACTORS[unit],
        columns=tuple(cols), rows=tuple(rows),
        pages=tuple(sorted({r.page_no for r in rows})), title=title,
        period_source=source,
    ), ""


def _parse_rows(
    body: list[LogicalLine],
    columns: list[Column],
    page_index: dict[int, int],
    page_no: int,
) -> list[M.ParsedRow]:
    """按列切出一行行数据。

    只保留「至少有一列读出数值」的行，分类标题行（`流动资产：`）自然被排除——
    不需要维护一张「哪些是标题」的名单，那种名单每换一家公司就得重写。
    """
    page_of: dict[int, int] = {k: page_no + i + 1 for k, i in page_index.items()}

    out: list[M.ParsedRow] = []
    for ln in body:
        cells = _value_cells(ln)
        if not cells:
            continue
        n = len(columns)
        values: list[Decimal | None] = [None] * n
        dashes: list[bool] = [False] * n
        hit = False
        for cell in cells:
            i = _column_of(cell, columns)
            if i is None:
                continue
            val, is_dash = parse_number(cell.text)
            if val is not None:
                values[i] = val
                hit = True
            elif is_dash:
                dashes[i] = True
        if not hit and not any(dashes):
            continue

        # 行名 = 第一个「非行名」单元格左边的全部文字。
        # 「非行名」包括**数值格和附注号格**——漏掉后者就会把 `(五)52`
        # 粘进行名里（见 NOTE_CELL_RE 的说明）。
        boundaries = [c.x0 for c in cells]
        note_cell = next((c for c in ln.cells() if _is_note_cell(c)), None)
        if note_cell is not None:
            boundaries.append(note_cell.x0)
        label = ln.text_in(0.0, min(boundaries) - 1.0)
        if not label:
            continue

        out.append(M.ParsedRow(
            label=label, note_ref=note_cell.text if note_cell else None,
            values=tuple(values), dashes=tuple(dashes),
            page_no=page_of.get(id(ln), page_no + 1),
            source_text=ln.full_text,
        ))
    return out


#: 从文件名里抠年份：'宝钢股份：2024年年度报告.pdf' / '首钢股份2023年年度报告.pdf'
_FILENAME_YEAR_RE = re.compile(r"(20\d{2})\s*年")


def parse_document(
    doc: pymupdf.Document, file_name: str, report_year: str | None = None
) -> M.ParseReport:
    """把一份年报解析成若干张报表（合并口径 + 母公司口径）。

    `report_year` 是**末位兜底**：极少数报表页既没有带年份的表头，也没有
    日期行（首钢 2024 有几张就是这样），这时只能从文件名或调用方给的年份推。
    推出来的期间会在 `period_source` 里标出来，不冒充「从文件里读到的」。
    """
    report = M.ParseReport(file=file_name, pages_total=doc.page_count)
    if report_year is None:
        m = _FILENAME_YEAR_RE.search(file_name)
        report_year = m.group(1) if m else None
    seen: set[tuple[str, str]] = set()

    for page_no, title, kind, scope in _title_lines(doc):
        if (kind, scope) in seen:
            # 同一张表在一份年报里出现多次是正常的（主要指标表、财务摘要），
            # 正文里第一次出现的那份才带完整附注，取它。
            report.skipped_duplicates += 1
            continue
        stmt, why = _parse_one(doc, page_no, title, kind, scope, report_year)
        if stmt is None:
            report.unparsed_titles.append((page_no + 1, f"{title}：{why}"))
            continue
        seen.add((kind, scope))
        report.statements.append(stmt)

    return report
