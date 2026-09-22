"""解析层的纯函数测试。

这些函数不碰 PDF、不碰数据库，所以可以拿真实年报里出现过的**原样字符串**
当用例——用例本身就是从 22 份年报里抄下来的。

为什么值得单测这些：它们每一个都对应过一次真实翻车，而且**每一处的失败
都是静默的**——不报错，只是某个字段整年没有数据，页面上显示成「未披露」。
"""

from __future__ import annotations

import pytest

from app.parsing.mapping import LabelIndex, normalize_label
from app.parsing.statements import NOTE_CELL_RE, parse_number


# ---------------------------------------------------------------- 数值


@pytest.mark.parametrize(
    "text,expected",
    [
        ("322,115,845,919.76", "322115845919.76"),
        ("-70,487,849.26", "-70487849.26"),      # 半角负号
        ("－70,487,849.26", "-70487849.26"),     # 全角负号
        ("(1,234.56)", "-1234.56"),              # 括号表负数
        ("（1,234.56）", "-1234.56"),
        ("0.00", "0.00"),
    ],
)
def test_parse_number_handles_every_notation(text: str, expected: str) -> None:
    value, is_dash = parse_number(text)
    assert value is not None and not is_dash
    assert str(value) == expected


@pytest.mark.parametrize("dash", ["-", "－", "—", "–", "―"])
def test_dash_is_not_zero(dash: str) -> None:
    """破折号是「本期无金额」，不是「金额为零」。

    当成 0 的话，它会以「值等于 0」的样子出现在比率里做分母，
    而 0 和「没有这一项」在会计上是两回事。
    """
    value, is_dash = parse_number(dash)
    assert value is None and is_dash


def test_parse_number_rejects_label_text() -> None:
    """行名不能被当成数值——否则分类标题行会在时间线上显示成一个数。"""
    for text in ("流动资产：", "其中：营业收入", "一、营业总收入", "2024年12月31日"):
        value, is_dash = parse_number(text)
        assert value is None and not is_dash


# ---------------------------------------------------------------- 附注号


@pytest.mark.parametrize(
    "text", ["51", "(五)52", "（六）1", "附注五52", "注1", "1", "12"]
)
def test_note_cells_are_recognised(text: str) -> None:
    """附注号不都是纯数字。

    宝钢 2024 写 `51`，宝钢 2015 写 `(五)52`。后者不是数字，于是既不算数值格，
    又落在行名区间里，把行名粘成 `其中：营业收入(五)52`——
    那一行整年映射不上，表现是「这两年没有营业收入数据」。
    """
    assert NOTE_CELL_RE.match(text.replace(" ", ""))


@pytest.mark.parametrize("text", ["营业收入", "流动资产：", "货币资金", "一、营业总收入"])
def test_real_labels_are_not_mistaken_for_note_cells(text: str) -> None:
    """反例：真实行名不能被当成附注号。"""
    assert not NOTE_CELL_RE.match(text.replace(" ", ""))


# ---------------------------------------------------------------- 行名归一化


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("其中：营业收入", "营业收入"),
        ("一、营业总收入", "营业总收入"),
        ("（一）按经营持续性分类", "按经营持续性分类"),
        ("五、现金及现金等价物净增加额", "现金及现金等价物净增加额"),
        ("加：其他收益", "其他收益"),
        ("减：所得税费用", "所得税费用"),
        ("三、营业利润（亏损以“－”号填列）", "营业利润"),
        ("五、净利润（净亏损以“－”号填列）", "净利润"),
        ("1、合并资产负债表", "合并资产负债表"),
    ],
)
def test_normalize_label_strips_hierarchy(raw: str, expected: str) -> None:
    """归一化剥掉的是**层级标记**，不是科目名本身。

    不归一化的话，实测 2430 行里只有 419 行能命中字典（17%）——
    不是字典写错了，是两边说的不是同一个字符串。
    """
    assert normalize_label(raw) == expected


def test_normalize_label_keeps_meaningful_parentheses() -> None:
    """只有「…号填列」这类填表说明会被剥掉。

    真实科目名里也有括号（`以公允价值计量且其变动计入当期损益的金融资产`），
    乱剥会让两个不同的科目归一成同一个名字。
    """
    assert normalize_label("应收票据及应收账款") == "应收票据及应收账款"
    assert normalize_label("所有者权益(或股东权益)合计") == "所有者权益(或股东权益)合计"


# ---------------------------------------------------------------- 匹配


def test_matching_is_exact_not_substring() -> None:
    """**精确匹配，不是包含匹配。**

    这是字段字典里写明的语义：`notes_and_ar`（应收票据及应收账款）的别名
    **包含** `应收账款`，而「应收账款」又是它自己的排除词。
    包含匹配下它会被自己永远挡住。
    """
    from app.db.session import connect_memory, init_schema, load_seeds

    con = connect_memory()
    init_schema(con)
    load_seeds(con)
    index = LabelIndex.from_db(con, industry="steel")
    con.close()

    assert index.match("应收账款").metric_key == "accounts_receivable"
    assert index.match("应收票据及应收账款").metric_key == "notes_and_ar"
    assert index.match("其他应收款").metric_key is None      # 字典里没有，不硬塞
    assert index.match("货币资金合计").metric_key is None     # 不是「货币资金」


# ---------------------------------------------------------------- 证据原文


def _line(*cells: tuple[str, float]) -> "LogicalLine":
    """按「每个单元格从某个 x 开始、字宽 5pt」造一个逻辑行。

    ⚠ 传进来的必须是 `Char`，不能是裸的三元组——`display_text` 读的是 `char.c`，
    传元组会得到 `'tuple' object has no attribute 'c'`，
    而这个报错出现在 `LogicalLine` 内部，看着像是实现坏了。
    """
    from app.parsing.reader import Char, LogicalLine

    chars: list[Char] = []
    for text, left in cells:
        for i, ch in enumerate(text):
            x0 = left + i * 5.0
            chars.append(Char(c=ch, x0=x0, x1=x0 + 5.0, y0=100.0))
    return LogicalLine(y=100.0, chars=tuple(chars))


class TestDisplayText:
    """`display_text`：证据面板里显示的「年报原文」。

    它和 `full_text` 的差别只有一个——**单元格之间补空格**。看着像排版小事，
    但 `source_text` 就是「点任意结论回到年报原文」里的那句原文：
    数字全对而糊成一串时，评审的第一反应是解析坏了。
    """

    def test_cells_are_separated_but_numbers_are_not(self):
        """列与列之间补空格；数字内部不补——`1,234.56` 拆成 `1, 234.56` 就毁了。"""
        line = _line(("营业收入", 50.0), ("1,234.56", 400.0))

        assert line.full_text == "营业收入1,234.56"        # 粘成一片
        assert line.display_text == "营业收入  1,234.56"   # 分得开，数字仍完整

    def test_wrapped_segment_is_not_reordered(self):
        """⚠ 折行段不能被按 x0 重排插回第一段中间。

        折行段的 x 回到左边（与行名同栏）。一旦按 x0 全局排序，第二段的字会
        **插进第一段数字的中间**，拼出一个年报上根本没有的句子——
        而它看起来完全正常，没有任何地方会报错。
        """
        # 顺序与 merge_wrapped 的输出一致：第一段整体在前，折行段接在后面
        line = _line(("以公允价值计量", 50.0), ("1234", 400.0), ("的金融资产", 50.0))

        assert line.display_text == "以公允价值计量  1234的金融资产"
        assert line.display_text.index("1234") < line.display_text.index("的金融资产")

    def test_empty_line(self):
        from app.parsing.reader import LogicalLine

        assert LogicalLine(y=0.0, chars=()).display_text == ""
