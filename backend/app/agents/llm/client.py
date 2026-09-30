"""模型客户端：发一次请求、拿到结构化 JSON、失败有 schema_repair 重试。

甲阶段一的验收标准就是这一句。三件事必须成立：

1. **结构化输出**。要求模型返回 JSON，解析前先剥掉常见的 ```json 围栏——
   模型很爱加围栏，为此把一次成功的调用判成失败毫无意义。
2. **schema_repair 重试**。解析失败或校验不过时，把**错误信息与原始输出**
   一起送回给模型让它改。最多重试 `max_repair_attempts` 次：无限重试会挂死，
   而吞掉第二次失败会把「模型改不好」变成「悄悄返回了坏数据」。
3. **每次调用都落库，失败路径也要**。证据链恰好在最需要它的时候（出错那次）
   断掉，是最糟糕的失败方式。

密钥从 LlmSettings 读，**绝不进 params、日志或返回值**。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from app.agents.llm.prompts.registry import Prompt, PromptRegistry
from app.agents.llm.settings import LlmSettings
from app.agents.llm.transport import (
    Completion,
    Message,
    Transport,
    TransportError,
)

# 模型爱把 JSON 包在 ```json ... ``` 里。剥围栏是常规操作，不是容错。
_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n(.*?)\n?\s*```\s*$", re.S)
# 退路：从一段话里抠出第一个 JSON 对象。
_OBJECT_RE = re.compile(r"\{.*\}", re.S)


class JsonParseError(Exception):
    """模型输出不是合法 JSON。会被送进 schema_repair 重试。"""


class SchemaError(Exception):
    """JSON 合法但不满足 schema。同样会被送进重试。"""


Validator = Callable[[dict[str, Any]], None]
"""校验函数：不通过就抛 SchemaError，异常消息会被送回去让模型改。"""


@dataclass(frozen=True)
class CallRecord:
    """一次模型调用的落库形态，对应 llm_call 表的一行。"""

    call_id: str
    purpose: str
    model: str
    model_version: str | None
    prompt_key: str
    prompt_version: str
    prompt_hash: str
    params: dict[str, Any]
    input_hash: str
    output: str | None
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int | None
    cached: bool
    cassette_id: str | None
    status: str                     # succeeded / failed / timeout
    error: str | None
    created_at: str
    attempt: int = 0
    task_id: str | None = None
    step_id: str | None = None


class CallSink(Protocol):
    """落库出口。skill 层把它接到 llm_call 表；测试里可以收到列表里。"""

    def __call__(self, record: CallRecord) -> None: ...


@dataclass(frozen=True)
class LlmResult:
    """一次 `complete_json` 的完整结果，含全部尝试的轨迹。"""

    data: dict[str, Any] | None
    call_id: str
    prompt_key: str
    prompt_version: str
    prompt_hash: str
    repaired: bool
    attempts: tuple[CallRecord, ...]

    @property
    def ok(self) -> bool:
        return self.data is not None

    @property
    def call_count(self) -> int:
        return len(self.attempts)


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里取出 JSON 对象。

    两级尝试：先剥围栏整体解析，再退一步抠出第一个 `{...}`。
    两者都失败才算解析失败——把一次本来成功的调用判成失败没有意义。
    """
    cleaned = text.strip()
    fenced = _FENCE_RE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()

    for candidate in (cleaned, *[m.group(0) for m in _OBJECT_RE.finditer(cleaned)]):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        raise JsonParseError(
            f"模型返回的 JSON 顶层是 {type(parsed).__name__}，要求是对象。"
            f"片段：{candidate[:120]}"
        )

    raise JsonParseError(
        f"输出不是合法 JSON。原始输出前 200 字：{text[:200]!r}"
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LlmClient:
    """带 schema_repair 的结构化输出客户端。

    传输层是注入的，所以「没有密钥」只影响能不能真发请求，
    不影响这套重试逻辑被完整地测到。
    """

    def __init__(
        self,
        *,
        settings: LlmSettings,
        transport: Transport,
        prompts: PromptRegistry,
        sink: CallSink | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.prompts = prompts
        self.sink = sink
        self._clock = clock or _utc_now

    # ------------------------------------------------------------ 主入口

    def complete_json(
        self,
        *,
        purpose: str,
        prompt_key: str,
        variables: dict[str, str],
        prompt_version: str | None = None,
        validator: Validator | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        extra_system: str | None = None,
    ) -> LlmResult:
        """渲染 prompt → 调模型 → 解析 JSON → 校验 → 不过就带着错误重试。

        每一轮**都**发一条 CallRecord 给 sink，无论成败。
        """
        prompt = self.prompts.get(prompt_key, prompt_version)
        rendered = prompt.render(variables)

        messages: list[Message] = []
        if extra_system:
            messages.append(Message(role="system", content=extra_system))
        messages.append(Message(role="user", content=rendered))

        input_hash = _sha256(
            "\x00".join(
                [
                    prompt.key,
                    prompt.version,
                    prompt.sha256,
                    rendered,
                    json.dumps(self.settings.public_params(), sort_keys=True),
                ]
            )
        )

        attempts: list[CallRecord] = []
        data: dict[str, Any] | None = None
        last_error: str | None = None

        for attempt in range(self.settings.max_repair_attempts + 1):
            call_id = _call_id(input_hash, attempt)
            try:
                completion = self.transport.complete(
                    messages,
                    model=self.settings.model,
                    temperature=self.settings.temperature,
                    seed=self.settings.seed,
                    timeout_s=self.settings.timeout_s,
                )
            except TransportError as exc:
                record = self._record(
                    call_id, purpose, prompt, input_hash, attempt, task_id, step_id,
                    output=None, completion=None, status="failed", error=str(exc),
                )
                attempts.append(record)
                self._emit(record)
                raise

            try:
                data = extract_json(completion.text)
                if validator is not None:
                    validator(data)
            except (JsonParseError, SchemaError) as exc:
                last_error = str(exc)
                record = self._record(
                    call_id, purpose, prompt, input_hash, attempt, task_id, step_id,
                    output=completion.text, completion=completion,
                    status="failed", error=last_error,
                )
                attempts.append(record)
                self._emit(record)

                # 把模型自己的输出 + 具体错因送回去让它改。
                # 只说「不对」不说哪里不对，模型基本改不动。
                messages.append(Message(role="assistant", content=completion.text))
                messages.append(
                    Message(
                        role="user",
                        content=(
                            f"上面的输出不满足要求，具体问题是：{last_error}\n"
                            f"请只返回修正后的 JSON 对象，不要任何解释或代码围栏。"
                        ),
                    )
                )
                continue

            record = self._record(
                call_id, purpose, prompt, input_hash, attempt, task_id, step_id,
                output=completion.text, completion=completion,
                status="succeeded", error=None,
            )
            attempts.append(record)
            self._emit(record)
            return LlmResult(
                data=data,
                call_id=call_id,
                prompt_key=prompt.key,
                prompt_version=prompt.version,
                prompt_hash=prompt.sha256,
                repaired=attempt > 0,
                attempts=tuple(attempts),
            )

        # 重试用尽仍不通过。**明确返回失败，不返回半成品数据。**
        return LlmResult(
            data=None,
            call_id=attempts[-1].call_id,
            prompt_key=prompt.key,
            prompt_version=prompt.version,
            prompt_hash=prompt.sha256,
            repaired=len(attempts) > 1,
            attempts=tuple(attempts),
        )

    # ------------------------------------------------------------ 内部

    def _record(
        self,
        call_id: str,
        purpose: str,
        prompt: Prompt,
        input_hash: str,
        attempt: int,
        task_id: str | None,
        step_id: str | None,
        *,
        output: str | None,
        completion: Completion | None,
        status: str,
        error: str | None,
    ) -> CallRecord:
        return CallRecord(
            call_id=call_id,
            purpose=purpose,
            model=self.settings.model,
            model_version=completion.model_version if completion else None,
            prompt_key=prompt.key,
            prompt_version=prompt.version,
            prompt_hash=prompt.sha256,
            params=self.settings.public_params(),   # 不含密钥
            input_hash=input_hash,
            output=output,
            tokens_in=completion.tokens_in if completion else None,
            tokens_out=completion.tokens_out if completion else None,
            latency_ms=completion.latency_ms if completion else None,
            cached=completion.cached if completion else False,
            cassette_id=completion.cassette_id if completion else None,
            status=status,
            error=error,
            created_at=self._clock(),
            attempt=attempt,
            task_id=task_id,
            step_id=step_id,
        )

    def _emit(self, record: CallRecord) -> None:
        if self.sink is not None:
            self.sink(record)

    # ------------------------------------------------------------ 便捷构造

    @classmethod
    def from_settings(
        cls,
        settings: LlmSettings,
        *,
        prompts_dir: Path,
        transport: Transport,
        sink: CallSink | None = None,
    ) -> "LlmClient":
        return cls(
            settings=settings,
            transport=transport,
            prompts=PromptRegistry(prompts_dir),
            sink=sink,
        )


def _call_id(input_hash: str, attempt: int) -> str:
    """确定性 call_id。

    确定性而不是随机：同一套输入重跑会得到同样的 call_id，`llm_call` 的
    主键因此能挡住重复写入，历史证据链也不会因为换个随机数就断掉。
    """
    return "llm-" + _sha256(f"{input_hash}:{attempt}")[:16]
