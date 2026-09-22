"""行名 → `metric_key`。

## 为什么必须先归一化

字段字典里的别名是**干净的行名**（`营业收入`、`现金及现金等价物净增加额`），
而年报里的行名带着层次结构：

    一、营业总收入
    其中：营业收入
    五、现金及现金等价物净增加额
    三、营业利润（亏损以“－”号填列）

直接精确比较的话，实测 2430 行里只命中 419 行（17%）——不是字典写错了，
是**两边说的不是同一个字符串**。

归一化剥掉的是**层级标记**（序号、「其中：」「加：」「减：」、
「（…号填列）」这类填表说明），不是数据本身。剥完仍然精确匹配，
不做包含匹配：`notes_and_ar`（应收票据及应收账款）的别名**包含**
`应收账款`，而「应收账款」又是它自己的排除词——包含匹配下它会被自己永远挡住。
这个坑在字段字典的设计里已经说明过。

## 匹配不上要报出来

映射不上的行名**全部返回给调用方**，不静默丢弃。

理由很实际：年报里一定存在字典没有的行（`流动资产合计`、`销售商品、提供劳务
收到的现金`……），它们本来就不该映射；但也一定存在**应该映射上却没映射上**的
（某年改了个说法、加了个括号）。两者都表现为「没匹配上」，靠人看清单才能分开。
静默丢弃的话，第二种会被当成第一种，而后果是某个字段**整年没有数据**——
页面上显示「未披露」，跟真实情况完全不同。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

#: 行首的层级序号：`一、` `（一）` `1.` `(3)` `1、`
_LEADING_NUMBER = re.compile(
    r"^\s*(?:"
    r"[一二三四五六七八九十百]+[、.．]"
    r"|（[一二三四五六七八九十百]+）"
    r"|\([一二三四五六七八九十百]+\)"
    r"|\d+[、.．]"
    r"|（\d+）|\(\d+\)"
    r")\s*"
)

#: 行首的层级连接词。`其中：营业收入` 就是「营业总收入」下面的「营业收入」。
_LEADING_CONNECTOR = re.compile(r"^\s*(?:其中|加|减|其他)[：:]\s*")

#: 行尾的填表说明。`营业利润（亏损以“－”号填列）` 说的是正负号怎么读，
#: 不是科目名的一部分。
_TRAILING_NOTE = re.compile(r"[（(][^（()）]*(?:号填列|以[“\"']?[-－—]?[”\"']?号)[^（()）]*[)）]\s*$")

#: 行内的空白。中文 PDF 常在字符间插空格（`2024 年12 月31 日`）。
_SPACES = re.compile(r"\s+")


def normalize_label(text: str) -> str:
    """把年报里的行名归一化成字典里那种干净形式。

    三步，顺序不能换：
      1. 去空白（PDF 抽出来的文字常带空格）
      2. 剥行首序号与连接词（可以叠：`（一）其中：营业收入`）
      3. 剥行尾填表说明

    第 3 步放在最后：`营业利润（亏损以“－”号填列）` 要先去掉序号才轮到它，
    而带括号的序号（`（一）`）又必须在这一步之前处理掉，否则会被当成填表说明。
    """
    s = _SPACES.sub("", text)
    for _ in range(3):  # 最多剥三层，防病态输入死循环
        before = s
        s = _LEADING_NUMBER.sub("", s)
        s = _LEADING_CONNECTOR.sub("", s)
        if s == before:
            break
    s = _TRAILING_NOTE.sub("", s)
    return s.strip()


@dataclass
class MatchResult:
    """一条行名的映射结果。"""

    metric_key: str | None
    matched_on: str | None = None      # 命中的是哪个别名（便于追查字典）


@dataclass
class LabelIndex:
    """行名 → metric_key 的索引。**精确匹配**，不做包含。"""

    #: 归一化后的别名 → (metric_key, 原始别名)
    by_alias: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: 同一别名挂在多个字段上的（字典校验会拦，这里留个记录）
    conflicts: list[str] = field(default_factory=list)

    @classmethod
    def from_db(cls, con: sqlite3.Connection, industry: str | None = None) -> "LabelIndex":
        """从字段字典装载。

        `industry` 用来先过滤候选：同一别名挂在两个字段上是允许的，
        只要它们属于不同行业（由 `project.industry` 区分）。
        """
        idx = cls()
        sql = "SELECT metric_key, aliases, industry FROM metric_definition"
        args: tuple = ()
        if industry is not None:
            sql += " WHERE industry IS NULL OR industry = ? OR industry = ''"
            args = (industry,)
        for row in con.execute(sql, args):
            for alias in json.loads(row["aliases"] or "[]"):
                key = normalize_label(alias)
                if not key:
                    continue
                if key in idx.by_alias and idx.by_alias[key][0] != row["metric_key"]:
                    idx.conflicts.append(key)
                    continue
                idx.by_alias[key] = (row["metric_key"], alias)
        return idx

    def match(self, label: str) -> MatchResult:
        hit = self.by_alias.get(normalize_label(label))
        if hit is None:
            return MatchResult(metric_key=None)
        return MatchResult(metric_key=hit[0], matched_on=hit[1])
