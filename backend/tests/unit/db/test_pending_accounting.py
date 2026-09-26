"""`rule_config` 里「已实现但没签字」的参数，与 `待会计确认.md` 必须对得上。

## 为什么需要这份清单

`tier` 只有三档（A-8：`hard` / `soft` / `model`），**没有「实现时先拍了、
还没经会计确认」这一档**。于是情景权重、状态计分权重、估值上下限这些
实现时自己定的数字，因为 `tier` 默认 `hard`，在库里与其他会计口径的参数
**长得完全一样**。机器分不出来，只能靠 `description` 的
`⬜ 待会计确认` 前缀 + 仓库根目录那份人工清单。

## ⚠ 这两边会静默分叉

  · 种子里加了标记、忘了写进清单 → 清单看起来是全的，实际漏了一条
  · 清单里写了、种子上的标记被「顺手清理」掉 → 参数看起来已签字，实际没有

两种都不报错，而且第二种更危险：**信号消失比信号错误更难发现**。
所以在这里对账，而不是靠人记得。
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from app.db.session import BACKEND_DIR, connect_memory, init_schema, load_seeds

#: 标记前缀。改这里要**同时**改种子文件与 `待会计确认.md`——
#: 只改一处，本文件的断言会失败，这是刻意的。
MARKER = "⬜ 待会计确认"

#: 人工清单。用真实文件路径而不是常量：它是给人和 AI 读的，
#: 必须真的在仓库根目录能被打开。
PENDING_DOC = BACKEND_DIR.parent / "待会计确认.md"

#: 清单里的章节标题，表格解析靠它定位。
_TABLE_HEADING = "## 被标记的参数"


@pytest.fixture()
def con() -> sqlite3.Connection:
    c = connect_memory()
    init_schema(c)
    load_seeds(c)
    yield c
    c.close()


def _marked_keys(con: sqlite3.Connection) -> set[str]:
    """种子里带标记的参数。断言打在**真实种子数据**上，不是构造样例。"""
    return {
        r["key"]
        for r in con.execute(
            "SELECT key FROM rule_config WHERE description LIKE ?", (f"%{MARKER}%",)
        )
    }


def _documented_keys() -> set[str]:
    """清单表格里列出的参数。

    只读 `## 被标记的参数` 那一节到下一个 `---` 之间，
    免得正文里顺口提到的键名被当成「已登记」。
    """
    text = PENDING_DOC.read_text(encoding="utf-8")
    start = text.index(_TABLE_HEADING)
    end = text.index("\n---", start)
    return set(re.findall(r"^\|\s*`([a-z][a-z0-9_.]*)`", text[start:end], re.M))


def test_the_pending_doc_exists_and_lists_something() -> None:
    assert PENDING_DOC.exists(), (
        f"人工清单不在：{PENDING_DOC}\n"
        "它记录了「已实现但未经会计确认」的参数，删掉它等于让这批参数"
        "在库里伪装成已签字的口径。"
    )
    assert _documented_keys(), f"{PENDING_DOC.name} 的「{_TABLE_HEADING}」一节没解析出任何参数"


def test_every_marked_param_is_documented(con: sqlite3.Connection) -> None:
    """种子里标了「待确认」，清单里就必须有。"""
    missing = sorted(_marked_keys(con) - _documented_keys())
    assert not missing, (
        f"这些参数标着「{MARKER}」但没写进 {PENDING_DOC.name}：{missing}\n"
        "会计同学只会看那份清单，漏写的等于没提。"
    )


def test_every_documented_param_is_marked(con: sqlite3.Connection) -> None:
    """清单里登记了，种子里就必须还带着标记。

    ⚠ 这条挡的是**「顺手清理」**：把标记删掉看起来像打扫卫生，
    实际是单方面宣布这个参数已经过会计确认。
    """
    extra = sorted(_documented_keys() - _marked_keys(con))
    assert not extra, (
        f"这些参数在 {PENDING_DOC.name} 里登记着，但种子里的标记没了：{extra}\n"
        "去掉标记 = 声明它已经过会计确认。真确认了就把它从清单里删掉，"
        "两件事要一起做。"
    )


def test_the_marked_set_is_not_empty(con: sqlite3.Connection) -> None:
    """标记被整批摘掉时，上面两条会**双双通过**（空集等于空集）。

    所以单独立一条守下限。数量变化本身不报错——签掉一条少一条是正常的，
    但它不能变成 0：估值情景权重那批一天没签字，这里就一天不该是空的。
    """
    assert len(_marked_keys(con)) >= 12, (
        "带标记的参数少于 12 条了。如果确实签掉了一批，把这条断言的下限一起改；"
        "如果没改过，那说明标记被误删了。"
    )


def test_marker_never_leaks_into_a_value(con: sqlite3.Connection) -> None:
    """标记只能出现在 `description` 里。

    漏进 `value` 的话，读参数的地方会拿到一句带中文前缀的字符串——
    `Decimal('⬜ 待会计确认（问题 6）｜0.02')` 会抛异常，而
    `float()` 之类的地方可能**静默变成 0 或 NaN**。
    """
    bad = [
        r["key"]
        for r in con.execute("SELECT key, value FROM rule_config")
        if MARKER in (r["value"] or "")
    ]
    assert not bad, f"这些参数的值里混进了标记文本：{bad}"


def test_pending_params_are_all_hard_tier(con: sqlite3.Connection) -> None:
    """待确认的是**会计口径**，不是软规则。

    标成 `soft` 的话，`test_hard_rules_carry_the_full_signoff_set` 就管不到它们，
    而它们在界面上仍然决定估值区间——降级会让它们绕开那套检查。
    """
    soft = sorted(
        r["key"]
        for r in con.execute(
            "SELECT key, tier FROM rule_config WHERE description LIKE ?", (f"%{MARKER}%",)
        )
        if r["tier"] != "hard"
    )
    assert not soft, f"这些参数待会计确认，却被降级成非 hard：{soft}"
