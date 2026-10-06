"""`--rekey`：编号漂了以后，按原文把会计填的东西救回来。

★ 它存在的理由是 2026-10-06 真发生的一次事故：

    为了给 `claim_match` 加几列跑了 `init_db.py --force`，
    会计交回来的 121 行**一行都对不上**。

`claim_id` = `sha1(项目|章节|句序|原文)`。原文是稳的，
**章节序号与句序不稳**——重新解析一次、或者分段规则改一改，
后面所有 id 就平移了。文档里那句「确定性 id」在同一次解析内成立，
跨一次解析就不成立，而这两件事长得一模一样。

所以这个工具要守住的东西只有一条：**能救就救，救不了不许猜。**
猜错的话那一行的判定会挂到别的主张上，而两行的值看起来都正常。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

#: 仓库根。脚本在 `<repo>/scripts/`，不在 `backend/scripts/`——
#: 这两处**各有一份 scripts 目录**，取错一级的表现是 FileNotFoundError，
#: 而找起来要翻好几层。
ROOT = Path(__file__).resolve().parents[4]


def _load_script():
    """按路径加载 `scripts/export_input_templates.py`。

    ⚠ 不能直接 `import export_input_templates`：那个模块在 import 时
    往 sys.path 里塞了几个目录，而测试进程的 sys.path 里已经有
    `backend/`，两边打架时表现成「import 到了另一个同名模块」。
    """
    path = ROOT / "scripts" / "export_input_templates.py"
    spec = importlib.util.spec_from_file_location("_eit_for_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_eit_for_test"] = mod
    spec.loader.exec_module(mod)
    return mod


eit = _load_script()


@pytest.fixture()
def con() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE claim (claim_id TEXT PRIMARY KEY, claim_text TEXT,"
        " extractor TEXT)"
    )
    yield c
    c.close()


def _add(con, cid, text, extractor="rule:claim_v1"):
    con.execute("INSERT INTO claim VALUES (?,?,?)", (cid, text, extractor))


# ---------------------------------------------------------------- 唯一定位


def test_a_unique_sentence_is_rekeyed(con):
    _add(con, "cl-new1", "2018年，宝钢股份计划营业成本2420亿元。")
    new_id, how = eit._match_by_text(eit._text_index(con), "2018年，宝钢股份计划营业成本2420亿元。")
    assert new_id == "cl-new1"
    assert how == "全等"


def test_whitespace_differences_do_not_matter(con):
    """归一化要和 `narrative._claim_id` 的 `" ".join(text.split())` 一致。
    两处不一致的话，同一句话会算出两个键，重编号永远命中不了。"""
    _add(con, "cl-new1", "公司   实现   营业收入 3,221 亿元。")
    new_id, _ = eit._match_by_text(
        eit._text_index(con), "公司 实现 营业收入 3,221 亿元。"
    )
    assert new_id == "cl-new1"


def test_a_truncated_sentence_still_matches_by_prefix(con):
    """★ CSV 里的原文截到 400 字，长句会被截断。

    不试前缀的话，最长的那几句永远重编号不了——而它们恰恰是最需要
    人工判断的那些（要素最全、字数最多）。
    """
    long_text = "公司" + "很长的正文" * 100 + "结论。"
    _add(con, "cl-long", long_text)
    new_id, how = eit._match_by_text(eit._text_index(con), long_text[:400])
    assert new_id == "cl-long"
    assert how == "前缀"


# ---------------------------------------------------------------- 不许猜


def test_a_sentence_matching_two_claims_is_refused(con):
    """★ **命中多条就不许猜。**

    猜错的话那一行的判定会挂到别的主张上，而两行的值看起来都正常——
    这正是本文件最不该出现的行为。
    """
    _add(con, "cl-dup1", "公司实现营业收入3,221亿元。")
    _add(con, "cl-dup2", "公司实现营业收入3,221亿元。")
    new_id, why = eit._match_by_text(
        eit._text_index(con), "公司实现营业收入3,221亿元。"
    )
    assert new_id is None
    assert "不猜" in why


def test_a_missing_sentence_is_refused(con):
    _add(con, "cl-new1", "库里只有这一句。")
    new_id, why = eit._match_by_text(eit._text_index(con), "这句话库里根本没有。")
    assert new_id is None
    assert "找不到" in why


def test_llm_claims_are_not_in_the_index(con):
    """索引只收规则法。

    P 表的候选只从规则法来（`claim_scope`）。把模型法也收进来，
    同一句话会同时命中两条——而两边的措辞往往一模一样。
    """
    _add(con, "cl-llm-1", "公司实现营业收入3,221亿元。", "llm:deepseek-chat@x")
    assert eit._text_index(con) == {}
    new_id, _ = eit._match_by_text(eit._text_index(con), "公司实现营业收入3,221亿元。")
    assert new_id is None


def test_an_empty_sentence_never_matches_by_prefix(con):
    """空串做前缀会命中**所有**主张。

    没有 `key and` 那个守卫的话，一行没有原文的记录会被判成
    「前缀命中 N 条」——运气好时提示「不猜」，运气不好时 N 恰好是 1
    就直接改错了。
    """
    _add(con, "cl-only", "库里唯一的一句话。")
    new_id, why = eit._match_by_text(eit._text_index(con), "")
    assert new_id is None, f"空原文被当成了前缀命中：{new_id}"
    assert "找不到" in why


# ---------------------------------------------------------------- 端到端


def test_rekey_rewrites_only_the_id_column(con, tmp_path, monkeypatch, capsys):
    """跑一遍完整的 --rekey：**只动 claim_id，别的列一个字都不能变。**"""
    _add(con, "cl-new1", "计划营业成本2420亿元。")
    out = tmp_path / "人工录入"
    out.mkdir()
    monkeypatch.setattr(eit, "OUT_DIR", out)
    p = out / "P_人工确认.csv"
    p.write_text(
        "claim_id,claim_text,is_substantive,conclusion,reviewer\n"
        "↑ 编号,↑ 原文,↑ 是/否,↑ 结论,↑ 复核人\n"
        "cl-old1,计划营业成本2420亿元。,1,no_penalty,ChatGPT\n",
        encoding="utf-8-sig",
    )

    assert eit._rekey(con) == 0
    lines = p.read_text(encoding="utf-8-sig").splitlines()
    assert lines[1].startswith("↑"), "说明行被吃掉了——会计下次打开会看到一列裸英文列名"
    row = lines[2].split(",")
    assert row[0] == "cl-new1"
    assert row[1:] == ["计划营业成本2420亿元。", "1", "no_penalty", "ChatGPT"], (
        "除了编号，别的列一个字都不该动"
    )


def test_rekey_leaves_unmatchable_rows_alone(con, tmp_path, monkeypatch, capsys):
    """换不了的行**保持原样**，并让退出码非零——不能假装成功。"""
    _add(con, "cl-new1", "计划营业成本2420亿元。")
    out = tmp_path / "人工录入"
    out.mkdir()
    monkeypatch.setattr(eit, "OUT_DIR", out)
    p = out / "P_人工确认.csv"
    p.write_text(
        "claim_id,claim_text,conclusion\n"
        "↑ 编号,↑ 原文,↑ 结论\n"
        "cl-old1,计划营业成本2420亿元。,no_penalty\n"
        "cl-old2,这句库里压根没有。,p_penalty\n",
        encoding="utf-8-sig",
    )

    assert eit._rekey(con) == 1, "有换不了的行时必须返回非零"
    lines = p.read_text(encoding="utf-8-sig").splitlines()
    assert lines[2].split(",")[0] == "cl-new1"
    assert lines[3].split(",")[0] == "cl-old2", "换不了的行不许被改，也不许被删"
    assert "cl-old2" in capsys.readouterr().out
