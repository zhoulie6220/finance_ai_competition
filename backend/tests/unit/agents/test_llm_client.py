"""模型客户端的测试：结构化 JSON、schema_repair 重试、失败也要落库。

全部用 FakeTransport 与 ReplayTransport，**不联网也不需要密钥**。
这正是把传输层做成可注入的理由——这套重试逻辑值得单独测，
而它跟「能不能连上 DeepSeek」是两回事。

重点盯两类静默失败：
  · 重试次数写错（多调了几次没人知道）
  · 失败路径没落库（证据链恰好在最需要它的时候断掉）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.llm.client import (
    CallRecord,
    JsonParseError,
    LlmClient,
    SchemaError,
    extract_json,
)
from app.agents.llm.prompts.registry import PromptError, PromptRegistry
from app.agents.llm.settings import LlmSettings
from app.agents.llm.transport import (
    CassetteMissing,
    CassetteStore,
    Completion,
    Message,
    ReplayTransport,
    TransportError,
    cassette_key,
)
from app.agents.llm.transport import FakeTransport

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "app" / "agents" / "llm" / "prompts"


def settings(**overrides) -> LlmSettings:
    base = dict(
        model="deepseek-chat",
        temperature=0.0,
        seed=42,
        api_key="sk-test-not-a-real-key",
        max_repair_attempts=1,
    )
    base.update(overrides)
    return LlmSettings(**base)


def make_client(transport, *, collect: list | None = None, **overrides) -> LlmClient:
    sink = collect.append if collect is not None else None
    return LlmClient(
        settings=settings(**overrides),
        transport=transport,
        prompts=PromptRegistry(PROMPTS_DIR),
        sink=sink,
        clock=lambda: "2026-09-26T00:00:00+00:00",
    )


VARS = {"metric_keys": "revenue\noperating_cost", "period": "2024",
        "section_heading": "经营情况讨论", "section_text": "本期营业收入同比增长。"}


# ---------------------------------------------------------------- 注册表


def test_registry_loads_and_hashes():
    reg = PromptRegistry(PROMPTS_DIR)
    assert "claim_extract" in reg.keys()
    # ⚠ **不要写死版本列表**：新增一个版本就会让这条测试红，
    # 而「新增版本」是设计里明确鼓励的动作（改动一律新增版本号）。
    # 该断言的是「v1 还在」（旧版本不许删），不是「只有 v1」。
    assert "v1" in reg.versions("claim_extract")
    assert len(reg.registry_hash()) == 64


def test_registry_hash_changes_when_content_changes(tmp_path):
    """整表哈希是「这次运行与上次是不是同一套提示词」的判据。

    它要能在**任何一个 prompt 改了一个字**之后变掉。
    """
    src = (PROMPTS_DIR / "claim_extract.v1.md").read_text(encoding="utf-8")
    (tmp_path / "claim_extract.v1.md").write_text(src, encoding="utf-8")
    before = PromptRegistry(tmp_path).registry_hash()

    (tmp_path / "claim_extract.v1.md").write_text(src + "\n", encoding="utf-8")
    assert PromptRegistry(tmp_path).registry_hash() != before


def test_missing_placeholder_is_rejected():
    """★ 占位符没填全必须报错，不能替换成空串。

    替换成空串的话，模型收到一个语义残缺的提示词，照样会输出一段看起来
    合理的话，而没有任何地方会报错。
    """
    reg = PromptRegistry(PROMPTS_DIR)
    with pytest.raises(PromptError, match="缺少变量"):
        reg.get("claim_extract").render({"period": "2024"})


def test_unknown_prompt_and_version_are_rejected():
    reg = PromptRegistry(PROMPTS_DIR)
    with pytest.raises(PromptError):
        reg.get("no_such_prompt")
    with pytest.raises(PromptError):
        reg.get("claim_extract", "v99")


# ---------------------------------------------------------------- JSON 解析


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '好的，结果是：{"a": 1} 以上。',
        '  \n{"a": 1}\n  ',
    ],
)
def test_extract_json_accepts_common_shapes(text):
    """模型很爱加代码围栏或前后缀。为此把一次成功的调用判成失败没有意义。"""
    assert extract_json(text) == {"a": 1}


@pytest.mark.parametrize("text", ["not json at all", "[1, 2, 3]", "42", ""])
def test_extract_json_rejects_non_objects(text):
    with pytest.raises(JsonParseError):
        extract_json(text)


# ---------------------------------------------------------------- 重试


def test_happy_path_makes_exactly_one_call():
    transport = FakeTransport(replies=['{"claims": []}'])
    track: list[CallRecord] = []
    result = make_client(transport, collect=track).complete_json(
        purpose="claim_extract", prompt_key="claim_extract", variables=VARS
    )
    assert result.ok
    assert result.data == {"claims": []}
    assert result.call_count == 1
    assert result.repaired is False
    assert len(transport.calls) == 1
    assert len(track) == 1
    assert track[0].status == "succeeded"


def test_schema_repair_retries_once_and_succeeds():
    """★ 甲阶段一的验收标准：失败有 schema_repair 重试。

    第一次返回坏 JSON，第二次改对。**恰好两次调用**——多一次少一次都要能被发现。
    """
    transport = FakeTransport(replies=["这不是 JSON", '{"claims": []}'])
    track: list[CallRecord] = []
    result = make_client(transport, collect=track).complete_json(
        purpose="claim_extract", prompt_key="claim_extract", variables=VARS
    )
    assert result.ok
    assert result.call_count == 2
    assert result.repaired is True
    assert len(transport.calls) == 2
    assert [r.status for r in track] == ["failed", "succeeded"]


def test_repair_prompt_carries_the_error_and_the_bad_output():
    """重试时必须把**原始输出与具体错因**一起送回去。

    只说「不对」不说哪里不对，模型基本改不动，重试就变成纯浪费。
    """
    transport = FakeTransport(replies=["这不是 JSON", '{"claims": []}'])
    make_client(transport).complete_json(
        purpose="claim_extract", prompt_key="claim_extract", variables=VARS
    )
    second = transport.calls[1]
    assert any(m.role == "assistant" and m.content == "这不是 JSON" for m in second)
    repair_msg = second[-1].content
    assert "JSON" in repair_msg


def test_repair_exhausted_returns_failure_not_partial_data():
    """★ 用尽重试后必须**明确失败**，不许返回半成品。

    吞掉第二次失败会把「模型改不好」变成「悄悄返回了坏数据」——
    而下游会照常把它当成结论用。
    """
    transport = FakeTransport(replies=["坏的", "还是坏的"])
    track: list[CallRecord] = []
    result = make_client(transport, collect=track).complete_json(
        purpose="claim_extract", prompt_key="claim_extract", variables=VARS
    )
    assert result.ok is False
    assert result.data is None
    assert result.call_count == 2          # 初次 + 1 次修复，不多不少
    assert [r.status for r in track] == ["failed", "failed"]
    assert all(r.error for r in track)


def test_max_repair_attempts_zero_makes_one_call():
    """允许关掉重试，此时只调用一次。"""
    transport = FakeTransport(replies=["坏的"])
    result = make_client(transport, max_repair_attempts=0).complete_json(
        purpose="claim_extract", prompt_key="claim_extract", variables=VARS
    )
    assert result.ok is False
    assert result.call_count == 1


def test_validator_failure_triggers_repair():
    """JSON 合法但不满足 schema 时同样要重试，并把校验错误送回去。"""
    transport = FakeTransport(replies=['{"claims": "不是数组"}', '{"claims": []}'])

    def validator(data: dict) -> None:
        if not isinstance(data.get("claims"), list):
            raise SchemaError("claims 必须是数组")

    result = make_client(transport).complete_json(
        purpose="claim_extract",
        prompt_key="claim_extract",
        variables=VARS,
        validator=validator,
    )
    assert result.ok
    assert result.repaired is True
    assert "claims 必须是数组" in transport.calls[1][-1].content


# ---------------------------------------------------------------- 落库


def test_every_attempt_is_recorded_including_failures():
    """★ 失败路径也要落库。

    证据链恰好在最需要它的时候（出错那次）断掉，是最糟糕的失败方式。
    """
    transport = FakeTransport(replies=["坏的", "还是坏的"])
    track: list[CallRecord] = []
    make_client(transport, collect=track).complete_json(
        purpose="claim_extract", prompt_key="claim_extract", variables=VARS
    )
    assert len(track) == 2
    assert track[0].output == "坏的"          # 坏输出本身要留档
    # 版本号**不写死**——只要求「落库的版本与注册表当前给的一致」。
    # 写死 v1 的话，新增 v2 会让这条测试红，而那恰好是它不该管的事。
    expected = PromptRegistry(PROMPTS_DIR).get("claim_extract").version
    assert all(r.prompt_version == expected for r in track)
    assert all(len(r.prompt_hash) == 64 for r in track)


def test_transport_error_propagates_and_is_recorded():
    transport = FakeTransport(replies=[], fail_with=TransportError("网络断了"))
    track: list[CallRecord] = []
    with pytest.raises(TransportError):
        make_client(transport, collect=track).complete_json(
            purpose="claim_extract", prompt_key="claim_extract", variables=VARS
        )
    assert len(track) == 1
    assert track[0].status == "failed"
    assert "网络断了" in (track[0].error or "")


def test_secrets_never_reach_params():
    """★ 密钥只从 LlmSettings 读，绝不进 params / 日志 / 返回值。"""
    transport = FakeTransport(replies=['{"claims": []}'])
    track: list[CallRecord] = []
    make_client(transport, collect=track).complete_json(
        purpose="claim_extract", prompt_key="claim_extract", variables=VARS
    )
    blob = str(track[0].params)
    assert "sk-" not in blob
    assert "api_key" not in blob
    assert set(track[0].params) == {"model", "temperature", "seed", "offline_mode"}


def test_call_ids_are_deterministic():
    """同一套输入重跑得到同样的 call_id。

    确定性让 llm_call 的主键能挡住重复写入，历史证据链也不会因为换个随机数就断掉。
    """
    def run() -> str:
        return make_client(FakeTransport(replies=['{"claims": []}'])).complete_json(
            purpose="claim_extract", prompt_key="claim_extract", variables=VARS
        ).call_id

    assert run() == run()


# ---------------------------------------------------------------- 离线回放


def test_replay_returns_pre_recorded_response(tmp_path):
    store = CassetteStore(tmp_path)
    messages = [Message(role="user", content="你好")]
    key = cassette_key(messages, model="deepseek-chat", temperature=0.0, seed=42)
    store.write(key, Completion(text='{"claims": []}', model_version="deepseek-chat"))

    out = ReplayTransport(store).complete(
        messages, model="deepseek-chat", temperature=0.0, seed=42, timeout_s=10
    )
    assert out.text == '{"claims": []}'
    assert out.cached is True
    assert out.cassette_id == key


def test_missing_cassette_fails_loudly(tmp_path):
    """★ 回放时找不到预录响应必须显式报错。

    **绝不降级成真实请求**（断网的现场会突然开始联网），
    **也绝不返回空串**（会产出一段空白但看起来正常的输出）。
    """
    transport = ReplayTransport(CassetteStore(tmp_path))
    with pytest.raises(CassetteMissing, match="不会退回真实请求"):
        transport.complete(
            [Message(role="user", content="没录过的输入")],
            model="deepseek-chat",
            temperature=0.0,
            seed=42,
            timeout_s=10,
        )


def test_settings_mode_and_description():
    assert settings(offline_mode=True).mode == "replay"
    assert settings(offline_mode=False).mode == "live"
    assert "离线回放模式" in settings(offline_mode=True).describe()
    assert settings(api_key=None).configured is False
    # .env.example 里的占位值不算配好
    assert settings(api_key="sk-在此填入你的密钥").configured is False
