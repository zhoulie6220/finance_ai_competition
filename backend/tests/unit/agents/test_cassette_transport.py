"""传输层的录制 / 回放语义。

`RecordingTransport` 的取舍值得单独钉住，因为它**只体现在账单上**：
多调一次真实接口，进度照走、数字照涨、结果一模一样，从任何输出里都看不出来。
"""

from __future__ import annotations

from pathlib import Path

from app.agents.llm.transport import (
    CassetteMissing,
    CassetteStore,
    FakeTransport,
    Message,
    RecordingTransport,
    ReplayTransport,
    cassette_key,
)

CALL = dict(model="deepseek-chat", temperature=0.0, seed=42, timeout_s=5.0)


def _ask(transport, text: str):
    return transport.complete([Message(role="user", content=text)], **CALL)


def test_recording_transport_replays_instead_of_calling_again(tmp_path: Path) -> None:
    """★ 已经有磁带就直接放，不再拨一次号。

    抽取是分批跑的：跑一半断了、或中途加了新章节，重跑时前面那几百段
    会**原样再调一遍真实接口**——输入没变、temperature=0、seed 固定，
    结果必然一样。钱白花，而且**从输出上看不出来**。
    """
    store = CassetteStore(tmp_path)
    live = FakeTransport(replies=["第一次的回答"])
    rec = RecordingTransport(live, store)

    first = _ask(rec, "同一段正文")
    assert first.text == "第一次的回答"
    assert len(live.calls) == 1
    assert len(rec.recorded) == 1

    # 同样的输入再问一次
    second = _ask(rec, "同一段正文")
    assert second.text == "第一次的回答"
    assert len(live.calls) == 1, "已有磁带却又调了一次真实接口——白花钱"
    assert len(rec.replayed) == 1
    assert len(rec.recorded) == 1


def test_a_new_input_still_calls_live(tmp_path: Path) -> None:
    """上一条的反面。**没有这条，把「永远不调接口」当成通过也能测绿。**

    但 FakeTransport 的脚本用完会抛断言，所以这条同时也在守「别调多了」。
    """
    store = CassetteStore(tmp_path)
    live = FakeTransport(replies=["第一段", "第二段"])
    rec = RecordingTransport(live, store)

    assert _ask(rec, "第一段正文").text == "第一段"
    assert _ask(rec, "第二段正文").text == "第二段"
    assert len(live.calls) == 2
    assert len(rec.replayed) == 0


def test_replay_mode_still_refuses_a_missing_cassette(tmp_path: Path) -> None:
    """⚠ 录制层「先查磁带」**不许**传染给回放层。

    回放模式（现场断网时用的那个）找不到磁带必须报错，绝不降级成真实请求。
    两条路径共用一个 `CassetteStore`，「顺手也兜一下底」是很自然的改动——
    而它会让断网的现场突然开始联网，或者产出一段空白但看起来正常的输出。
    """
    store = CassetteStore(tmp_path)
    replay = ReplayTransport(store)
    assert replay is not None

    try:
        _ask(replay, "从来没录过的正文")
    except CassetteMissing as exc:
        assert "不会退回真实请求" in str(exc)
    else:  # pragma: no cover - 走到这里说明回放层被改坏了
        raise AssertionError("回放模式找不到磁带时没有报错")


def test_the_key_depends_on_the_input(tmp_path: Path) -> None:
    """磁带是按**输入内容**哈希存的，不是按顺序号。"""
    base = dict(model="deepseek-chat", temperature=0.0, seed=42)
    a = cassette_key([Message(role="user", content="甲")], **base)
    b = cassette_key([Message(role="user", content="乙")], **base)
    assert a != b

    # 参数变了也必须算另一盘磁带——否则改了 prompt 还在放旧磁带，
    # 而输出看起来完全正常
    c = cassette_key([Message(role="user", content="甲")], **{**base, "temperature": 0.7})
    assert a != c

    d = cassette_key([Message(role="user", content="甲")], **{**base, "seed": 7})
    assert a != d
