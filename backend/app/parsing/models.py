"""解析层的中间产物。

这一层刻意**不认识数据库**：它只把 PDF 变成「结构化但不含业务判断」的行，
落库、口径判定、与字典比对都在上层。分开的理由是解析可以被单独测试——
喂一份 PDF 进去，断言出来的行，不需要建库、不需要种子数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

#: 报表种类。与 `app/schemas/enums.py::Statement` 的取值对齐。
BALANCE = "balance"
INCOME = "income"
CASHFLOW = "cashflow"

#: 支持的报表版本。母公司口径要能识别出来并**排除**——
#: 它的表名（「母公司资产负债表」）包含合并口径表名（「资产负债表」）的子串，
#: 用 `in` 判断会把母公司报表当成合并报表读进去，而两者的数字完全不同。
SCOPE_CONSOLIDATED = "consolidated"
SCOPE_PARENT = "parent"

#: 表名 → (种类, 口径)。**精确匹配**，不做包含判断。
STATEMENT_TITLES: dict[str, tuple[str, str]] = {
    "合并资产负债表": (BALANCE, SCOPE_CONSOLIDATED),
    "合并利润表": (INCOME, SCOPE_CONSOLIDATED),
    "合并现金流量表": (CASHFLOW, SCOPE_CONSOLIDATED),
    "母公司资产负债表": (BALANCE, SCOPE_PARENT),
    "母公司利润表": (INCOME, SCOPE_PARENT),
    "母公司现金流量表": (CASHFLOW, SCOPE_PARENT),
    # 有些年报在合并表前加「合并及母公司」这样的字样，这里先不收——
    # 收错了比不收更糟：一段读错的数字会一路流进估值。
}

#: 单位 → 换算到「百万元」的因子。
#:
#: ⚠ 必须**逐表读**，不能按年报假设。实测宝钢 2015 一份年报里同时出现了
#:   万元、百万元、元、千元四种单位——不同表格用不同单位是常态。
#:   按错了因子，数字会差 10^n 倍，而**页面上看起来完全正常**，
#:   只有当两个不同来源的同一个指标对不上时才会露馅。
UNIT_FACTORS: dict[str, Decimal] = {
    "元": Decimal("0.000001"),
    "千元": Decimal("0.001"),
    "万元": Decimal("0.01"),
    "百万元": Decimal("1"),
}


@dataclass(frozen=True)
class ColumnSpec:
    """表头里的一列。"""

    raw_label: str
    #: 归一化后的会计期间，如 '2024'。取不到就是 None——**不猜**。
    period: str | None
    #: current（期间数）/ instant（时点数）
    period_kind: str
    #: 列在页面上的 x 区间（左闭右开），按表头词的位置推出来
    x_left: float
    x_right: float


@dataclass(frozen=True)
class ParsedRow:
    """报表里的一行。"""

    label: str
    #: 附注号（「附注五」那一列的取值），没有就是 None
    note_ref: str | None
    #: 与 `ParsedStatement.columns` 一一对应。None = 该单元格为空或为 '-'。
    values: tuple[Decimal | None, ...]
    #: 明确披露为 '-' 的列。与「空」分开记：
    #: 报表里的 '-' 意思是「本期无此项」，而空单元格多半是版面问题。
    #: 两者都不落库，但解析报告里要分开统计——混在一起就没法判断解析对不对。
    dashes: tuple[bool, ...]
    page_no: int
    source_text: str


@dataclass(frozen=True)
class ParsedStatement:
    """一张解析出来的报表。"""

    kind: str
    scope: str
    unit: str
    unit_factor: Decimal
    columns: tuple[ColumnSpec, ...]
    rows: tuple[ParsedRow, ...]
    pages: tuple[int, ...]
    title: str
    #: 期间是从哪儿来的：'表头年份' / '由报表日期 2024 推' / '由文件名年份 2024 推'。
    #: 记下来是因为后两种都是**推**的，不是读的——评审问「这个 2024 哪来的」
    #: 时要答得出来，而不是回一句「反正在文件里」。
    period_source: str = ""

    @property
    def is_consolidated(self) -> bool:
        return self.scope == SCOPE_CONSOLIDATED


@dataclass
class ParseReport:
    """一次解析的账。**解析必须能自证**：读了多少行、丢了多少行、为什么丢。"""

    file: str
    pages_total: int = 0
    statements: list[ParsedStatement] = field(default_factory=list)
    #: 表名找得到、但读不出结构（多半是版面与预期不符）
    unparsed_titles: list[tuple[int, str]] = field(default_factory=list)
    #: 同一张表出现多次（目录页、索引页）被跳过的次数
    skipped_duplicates: int = 0

    @property
    def rows(self) -> int:
        return sum(len(s.rows) for s in self.statements)
