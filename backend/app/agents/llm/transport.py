"""模型传输层：实时调用 / 录制 / 回放，三种模式共用一个接口。

为什么要把传输层单独抽出来
--------------------------
两个理由，都不是为了好看：

1. **断网演示。** 现场可能没有网。`OFFLINE_MODE=1` 时全部走 `backend/app/data/
   cassettes/` 下预录的响应，一个字节都不出网。docs/00 把「假装实时」列为诚信
   问题，所以回放模式下界面必须显著标注——`LlmSettings.describe()` 提供那句话。

2. **没有密钥也能把链路测完。** 「结构化 JSON + schema_repair 重试」这套逻辑
   值得单独测，而它跟「能不能连上 DeepSeek」是两回事。把传输层做成可注入的，
   用 `FakeTransport` 就能把重试、超时、坏 JSON 全测到，不需要联网也不需要密钥。

回放找不到 cassette 时**必须显式报错**，绝不降级成真实请求、也绝不返回空串——
前者会让断网的现场突然开始联网，后者会产出一段空白但看起来正常的输出。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

CASSETTE_SUFFIX = ".json"


class CassetteMissing(Exception):
    """回放模式下找不到对应的预录响应。

    这是个**必须让人看见**的错误：现场演示时它意味着这个场景没录过，
    而不是「模型说没查到」。
    """


class TransportError(Exception):
    """真实调用失败（网络、鉴权、超时）。"""


@dataclass(frozen=True)
class Message:
    role: str          # 'system' / 'user' / 'assistant'
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class Completion:
    """一次模型调用的原始结果。"""

    text: str
    model_version: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    cached: bool = False
    cassette_id: str | None = None


class Transport(Protocol):
    """传输层接口。实现类只需管「把消息发出去、把文本拿回来」。"""

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        seed: int | None,
        timeout_s: float,
    ) -> Completion: ...


# ---------------------------------------------------------------- cassette


def cassette_key(
    messages: Sequence[Message], *, model: str, temperature: float, seed: int | None
) -> str:
    """按请求内容算 cassette 标识。

    确定性哈希而不是时间戳：同样的输入必须落到同一个文件，否则「重跑一次
    应该得到同样的结果」这条就不成立了。
    """
    digest = hashlib.sha256()
    digest.update(f"{model}\x00{temperature}\x00{seed}\x00".encode())
    for m in messages:
        digest.update(f"{m.role}\x00{m.content}\x00".encode())
    return digest.hexdigest()[:32]


@dataclass
class CassetteStore:
    """预录响应的读写。一个 cassette 一个 JSON 文件，文件名即内容哈希。"""

    root: Path

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}{CASSETTE_SUFFIX}"

    def has(self, key: str) -> bool:
        return self.path_for(key).exists()

    def write(self, key: str, completion: Completion) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(key)
        payload = {
            "cassette_id": key,
            "model_version": completion.model_version,
            "text": completion.text,
            "tokens_in": completion.tokens_in,
            "tokens_out": completion.tokens_out,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def read(self, key: str) -> Completion:
        path = self.path_for(key)
        if not path.exists():
            raise CassetteMissing(
                f"离线回放模式下找不到预录响应：{path}。"
                f"该场景没有录过，**不会退回真实请求**——请先以录模式跑一次"
                f"（OFFLINE_MODE=0 且挂上 RecordingTransport），或确认输入与"
                f"录制当时完全一致（模型名、温度、种子、消息内容任一不同都会"
                f"算成另一个 cassette）。"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Completion(
            text=payload["text"],
            model_version=payload.get("model_version"),
            tokens_in=payload.get("tokens_in"),
            tokens_out=payload.get("tokens_out"),
            cached=True,
            cassette_id=payload.get("cassette_id", key),
        )


# ---------------------------------------------------------------- 三种传输实现


class ReplayTransport:
    """回放：只读 cassette，一个字节都不出网。"""

    def __init__(self, store: CassetteStore) -> None:
        self.store = store
        self.calls: list[str] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        seed: int | None,
        timeout_s: float,
    ) -> Completion:
        key = cassette_key(
            messages, model=model, temperature=temperature, seed=seed
        )
        self.calls.append(key)
        return self.store.read(key)


class RecordingTransport:
    """录制：转发给真实传输，同时把结果落成 cassette。"""

    def __init__(self, inner: Transport, store: CassetteStore) -> None:
        self.inner = inner
        self.store = store
        self.recorded: list[str] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        seed: int | None,
        timeout_s: float,
    ) -> Completion:
        completion = self.inner.complete(
            messages,
            model=model,
            temperature=temperature,
            seed=seed,
            timeout_s=timeout_s,
        )
        key = cassette_key(
            messages, model=model, temperature=temperature, seed=seed
        )
        self.store.write(key, completion)
        self.recorded.append(key)
        return completion


class OpenAiTransport:
    """真实调用：DeepSeek 走 OpenAI 兼容接口。

    `openai` 延迟导入——没装 SDK 时本模块仍可 import，其余两种模式照常可用。
    """

    def __init__(self, *, base_url: str, api_key: str) -> None:
        if not api_key:
            raise TransportError(
                "实时调用需要 LLM_API_KEY。请在 backend/.env 里填写"
                "（该文件已被 .gitignore 挡住），或把 OFFLINE_MODE 置 1 走回放。"
            )
        self.base_url = base_url
        self._api_key = api_key
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise TransportError(
                    "未安装 openai SDK。python -m pip install -r requirements.lock.txt"
                ) from exc
            self._client = OpenAI(base_url=self.base_url, api_key=self._api_key)
        return self._client

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        seed: int | None,
        timeout_s: float,
    ) -> Completion:
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [m.as_dict() for m in messages],
            "temperature": temperature,
            "timeout": timeout_s,
        }
        # seed 让「同输入同输出」更接近成立。DeepSeek 不一定支持，不支持时
        # 传了会被拒——所以只在给了值时才带这个参数。
        if seed is not None:
            kwargs["seed"] = seed

        started = time.monotonic()
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:  # SDK 的异常种类很多，统一转成一种
            raise TransportError(f"模型调用失败：{exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)

        usage = getattr(resp, "usage", None)
        return Completion(
            text=resp.choices[0].message.content or "",
            model_version=getattr(resp, "model", None),
            tokens_in=getattr(usage, "prompt_tokens", None),
            tokens_out=getattr(usage, "completion_tokens", None),
            latency_ms=elapsed_ms,
        )


@dataclass
class FakeTransport:
    """测试替身：按脚本依次返回预设文本。

    用它能把 schema_repair 的重试次数、坏 JSON、超时全测到，
    **不需要联网也不需要密钥**。脚本用完还继续调用就报错——
    否则「重试次数写错了」会表现为静默地多调几次，测不出来。
    """

    replies: list[str]
    model_version: str = "fake-model-1"
    fail_with: Exception | None = None
    calls: list[list[Message]] = field(default_factory=list)

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        seed: int | None,
        timeout_s: float,
    ) -> Completion:
        self.calls.append(list(messages))
        if self.fail_with is not None:
            raise self.fail_with
        if not self.replies:
            raise AssertionError(
                f"FakeTransport 的脚本用完了，但又被调用了第 {len(self.calls)} 次——"
                f"多半是重试逻辑的次数写错了"
            )
        return Completion(
            text=self.replies.pop(0),
            model_version=self.model_version,
            tokens_in=10,
            tokens_out=20,
            latency_ms=1,
        )


def build_transport(
    *,
    offline_mode: bool,
    base_url: str,
    api_key: str | None,
    cassette_dir: Path,
    record: bool = False,
) -> Transport:
    """按配置组装传输层。**模式判断只在这一处**。

    离线模式下无论 `record` 传什么都走回放——录制的语义是「要联网」，
    两者同时为真是配置矛盾，以离线为准更安全。
    """
    store = CassetteStore(cassette_dir)
    if offline_mode:
        return ReplayTransport(store)
    live = OpenAiTransport(base_url=base_url, api_key=api_key or "")
    if record:
        return RecordingTransport(live, store)
    return live
