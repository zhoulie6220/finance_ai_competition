"""P 表预判的边界与归一化。

**这个模块最要紧的测试是「它不能做什么」**——会计口径原话是
「模型只能提出候选，不能自动定 P」。所以这里盯三件事：

  · 预判**不产生 conclusion**，一行也不行
  · 预判落在 `p_prediction`，**不碰** `p_confirmation`
  · 模型回填错 claim_id 时不许错位写

三条都是静默的：值填对了、行数也对，只有「谁判的」错了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.llm.prompts.registry import PromptRegistry
from app.agents.llm.settings import LlmSettings
from app.agents.llm.transport import FakeTransport
from app.skills import p_predict

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "app" / "agents" / "llm" / "prompts"


# ---------------------------------------------------------------- 提示词


def test_the_confirm_prompt_exists_and_renders() -> None:
    reg = PromptRegistry(PROMPTS_DIR)
    assert "claim_confirm" in reg.keys()
    prompt = reg.get("claim_confirm")
    text = prompt.render({"claims": "- claim_id=cl-1\n  原文：公司销量增长。"})
    assert "公司销量增长。" in text
    # ⚠ 提示词里**不许出现**那两个枚举值——出现就说明在要模型定分，越权了。
    # （「扣分」这个词本身会出现，但是以「不做扣分判断」的形式，所以不查它。）
    for forbidden in ("p_penalty", "no_penalty"):
        assert forbidden not in text, f"提示词里出现了 {forbidden!r}：越权定分了"


def test_the_prompt_keeps_the_two_hard_limits() -> None:
    """口径 §4.2 的两条限制必须写在提示词里。

    少了它们，模型会把「句式雷同」和「愿景没兑现」当成套话扣分——
    而这两件事口径明令禁止。删掉这段文字不会有任何测试变红，
    所以在这里钉住。
    """
    text = PromptRegistry(PROMPTS_DIR).get("claim_confirm").text
    assert "兑现" in text, "少了「不能因为长期愿景没兑现而判套话」"
    assert "句式雷同" in text or "写得像模板" in text, "少了「句式像模板不等于套话」"


def test_the_prompt_asks_for_a_reason() -> None:
    """只给标签不给依据，复核就退化成「看着顺眼就点头」。"""
    text = PromptRegistry(PROMPTS_DIR).get("claim_confirm").text
    assert "reason" in text
    assert "指回原文" in text or "依据" in text


# ---------------------------------------------------------------- 归一化


def _result():
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class R:
        prompt_version: str = "v1"
        prompt_hash: str = "deadbeef" * 4
        call_id: str = "llm-test"

    return R()


def test_a_missing_boolean_falls_back_to_substantive() -> None:
    """模型没答上来时按「是实质表述」处理。

    两个方向都有代价：默认排除会**悄悄少算一条**（分母变小、P 虚高），
    默认纳入只是让会计多看一眼。宁可选后者。
    """
    row = p_predict._build_row("cl-1", {}, _result(), "deepseek-chat", "now")
    assert row[2] == 1, "判不出来时应当保守地纳入分母"


def test_only_known_elements_survive() -> None:
    """模型爱自造枚举值。不认识的一律丢掉——
    留着会让 `missing_elements` 里出现没人认识的词，而页面照常显示。"""
    row = p_predict._build_row(
        "cl-1",
        {"is_substantive": True, "missing_elements": ["metric", "科幻", "result"]},
        _result(),
        "deepseek-chat",
        "now",
    )
    assert json.loads(row[4]) == ["metric", "result"]


def test_booleans_are_accepted_in_the_shapes_models_actually_return() -> None:
    """模型有时给 true，有时给字符串 "true"，有时给 1。"""
    for raw, expected in [(True, 1), (False, 0), ("true", 1), ("否", 0), (1, 1)]:
        row = p_predict._build_row(
            "cl-1", {"is_substantive": raw}, _result(), "m", "now"
        )
        assert row[2] == expected, f"{raw!r} 被解析成了 {row[2]}"


def test_the_row_has_no_conclusion_column() -> None:
    """★ **结构上就写不进 conclusion。**

    这是本模块最重要的一条保证：`p_prediction` 表里没有那一列，
    构造出来的元组也不含它。会计口径要的「模型不能自动定 P」
    不是靠自觉，是靠**没地方写**。
    """
    row = p_predict._build_row(
        "cl-1",
        {"is_substantive": True, "is_template": True, "conclusion": "p_penalty"},
        _result(),
        "deepseek-chat",
        "now",
    )
    assert "p_penalty" not in row, "模型给的 conclusion 混进了落库数据"
    assert len(row) == 11, "列数与 p_prediction 的 INSERT 对不上"


def test_a_wrong_claim_id_cannot_be_written() -> None:
    """★ 模型回填的 id 必须在**这一批**里。

    它偶尔会串批或者编一个。不挡住的话，A 句的预判会写到 B 句头上——
    而两行的值看起来都正常，**谁也不会发现**。

    这里只测判据本身：`predict_for_project` 用 `by_id` 做这道闸门。
    """
    batch = [{"claim_id": "cl-1"}, {"claim_id": "cl-2"}]
    by_id = {c["claim_id"]: c for c in batch}
    returned = [
        {"claim_id": "cl-1"},
        {"claim_id": "cl-别的批次"},
        {"claim_id": "cl-1"},        # 同一批里重复回填
    ]
    accepted, seen = [], set()
    for j in returned:
        cid = j["claim_id"]
        if cid not in by_id or cid in seen:
            continue
        seen.add(cid)
        accepted.append(cid)
    assert accepted == ["cl-1"]


# ---------------------------------------------------------------- 客户端路径


class _FakeClient:
    """只实现 `complete_json`，用来跑通 `predict_for_project` 的落库路径。"""

    def __init__(self, judgements: list[dict], settings: LlmSettings):
        self._judgements = judgements
        self.settings = settings
        self.calls = 0

    def complete_json(self, *, purpose, prompt_key, variables, validator):
        self.calls += 1

        class R:
            ok = True
            data = {"judgements": self._judgements}
            prompt_version = "v1"
            prompt_hash = "x" * 64
            call_id = "llm-test"
            call_count = 1
            attempts: tuple = ()

        return R()


@pytest.fixture()
def db(tmp_path):
    """最小库：只建预判这条路要用的表。

    ⚠ **刻意不建 `p_confirmation`**——如果哪天有人把预判也往那边写，
    这里会直接崩，而不是悄悄多出一批「看起来像人确认过的」记录。
    """
    import sqlite3

    con = sqlite3.connect(tmp_path / "t.db")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE claim (
          claim_id TEXT PRIMARY KEY, project_id TEXT, claim_text TEXT,
          source_page INTEGER, verifiable INTEGER, claim_type TEXT,
          -- claim_scope 按它过滤来源与背景。**候选来源必须与导出、判定、
          -- 指数同源**，各写各的 WHERE 会让预判覆盖到一批不进指数的句子。
          -- ⚠ 这份手写的表结构必须跟着 `claim_scope.scope_sql` 用到的列走，
          -- 少一列不会「过滤失效」，是直接 `no such column` 报错——
          -- 那是好事，比静默少过滤强。
          extractor TEXT, background_only INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE p_prediction (
          id TEXT PRIMARY KEY, claim_id TEXT, is_substantive INTEGER,
          is_template INTEGER, missing_elements TEXT, reason TEXT,
          model TEXT, prompt_version TEXT, prompt_hash TEXT,
          llm_call_id TEXT, created_at TEXT, UNIQUE (claim_id)
        );
        INSERT INTO claim VALUES
          ('cl-1','p-x','公司销量增长 5%。',10,1,'demand','rule:claim_v1',0);
        INSERT INTO claim VALUES
          ('cl-2','p-x','行业形势复杂严峻。',11,1,'macro','rule:claim_v1',0);
        INSERT INTO claim VALUES
          ('cl-llm','p-x','模型法抽的句子。',12,1,'cost','llm:x@y',0);
        """
    )
    yield con
    con.close()


def test_prediction_lands_in_p_prediction_only(db) -> None:
    client = _FakeClient(
        [
            {"claim_id": "cl-1", "is_substantive": True, "is_template": False,
             "missing_elements": [], "reason": "有对象、期间、指标、结果"},
            {"claim_id": "cl-2", "is_substantive": False, "is_template": False,
             "missing_elements": ["object"], "reason": "讲的是行业不是公司"},
        ],
        LlmSettings(model="deepseek-chat", api_key="sk-test"),
    )
    summary = p_predict.predict_for_project(
        db, "p-x", now="2026-10-01T00:00:00+00:00", client=client
    )
    assert summary.claims_predicted == 2
    assert summary.not_returned == 0

    rows = {
        r["claim_id"]: dict(r) for r in db.execute("SELECT * FROM p_prediction")
    }
    assert rows["cl-1"]["is_substantive"] == 1
    assert rows["cl-2"]["is_substantive"] == 0
    assert rows["cl-2"]["reason"] == "讲的是行业不是公司"

    # 库里根本没有 p_confirmation 这张表——写过去会直接报错。
    assert not db.execute(
        "SELECT name FROM sqlite_master WHERE name='p_confirmation'"
    ).fetchone(), "夹具变了，这条测试的保证也就没了"


def test_a_claim_the_model_skipped_is_counted(db) -> None:
    """模型漏回一条必须**报数**。

    不报的话，会计看到的是「这一行没有预判」——和「这一行本来
    就不该有预判」长得一样。
    """
    client = _FakeClient(
        [{"claim_id": "cl-1", "is_substantive": True, "missing_elements": [],
          "reason": "有具体数字"}],
        LlmSettings(model="deepseek-chat", api_key="sk-test"),
    )
    summary = p_predict.predict_for_project(
        db, "p-x", now="2026-10-01T00:00:00+00:00", client=client
    )
    assert summary.claims_predicted == 1
    assert summary.not_returned == 1


def test_rerun_skips_existing_predictions(db) -> None:
    """幂等：重跑不重复花钱，也不覆盖已有预判。"""
    judgements = [{"claim_id": "cl-1", "is_substantive": True,
                   "missing_elements": [], "reason": "r"},
                  {"claim_id": "cl-2", "is_substantive": True,
                   "missing_elements": [], "reason": "r"}]
    client = _FakeClient(judgements, LlmSettings(model="m", api_key="sk-test"))
    p_predict.predict_for_project(db, "p-x", now="t", client=client)

    again = _FakeClient(judgements, LlmSettings(model="m", api_key="sk-test"))
    summary = p_predict.predict_for_project(db, "p-x", now="t", client=again)
    assert summary.claims_considered == 0
    assert summary.claims_skipped_existing == 2
    assert again.calls == 0, "已有预判却又调了一次——白花钱"


def test_background_claims_are_not_predicted(db) -> None:
    """★ 标成背景的主张**不进 P 候选**——这是 v1.1 的硬约束。

    「不可验证的主张必须标 background_only，**不得进入评分**」。
    预判是给 P 表做候选的，P 的分子只数 `p_penalty`，所以背景主张
    进了预判就等于**间接进了评分**。

    实测这条约束原来只写在 `match_and_store` 里，判定表守了、指数没守，
    宝钢有 33 条 `background_only=1` **却带着期间**的主张照样在算 H/C。
    """
    db.execute(
        "INSERT INTO claim VALUES"
        " ('cl-bg','p-x','一句背景表述。',13,1,'demand','rule:claim_v1',1)"
    )
    db.commit()

    client = _FakeClient(
        [{"claim_id": "cl-1", "is_substantive": True,
          "missing_elements": [], "reason": "r"},
         {"claim_id": "cl-2", "is_substantive": True,
          "missing_elements": [], "reason": "r"}],
        LlmSettings(model="m", api_key="sk-test"),
    )
    summary = p_predict.predict_for_project(db, "p-x", now="t", client=client)

    assert summary.claims_considered == 2, "背景主张混进了 P 候选"
    assert db.execute(
        "SELECT COUNT(*) FROM p_prediction WHERE claim_id = 'cl-bg'"
    ).fetchone()[0] == 0


def test_fake_transport_is_not_used_here() -> None:
    """留个提示：本模块的测试一律走 `_FakeClient`，不碰网络。

    真正需要 `FakeTransport` 的场景（schema_repair 重试）在
    `tests/unit/agents/test_llm_client.py` 里测，不在这里重复。
    """
    assert FakeTransport is not None
