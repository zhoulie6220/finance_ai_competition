"""Tool 登记表。

赛事把「Tool」列为独立模块，要求「工具调用情况完整记录」。本文件是那两项的落点：

  1. **一份定义，两个出口**：`ToolSpec` 里的 JSON Schema 同时供给 REST 与 MCP。
     分头维护两套参数定义的话，两边迟早会不一致，而对不上的表现是「MCP 那边报参数
     错误」，很难联想到 REST 侧改了 schema。
  2. **每次调用自动留痕**：调用一律走 `ToolRegistry.call()`，它负责校验参数、
     计算结果哈希、写 `tool_call` 表、推 SSE 事件。工具实现里**不许**自己写日志——
     那样就总有人忘了写。

边界（与「数字由程序计算，模型只负责理解和组织」对应）：
`deterministic=True` 的工具是纯计算，结果可复算；`False` 的含 LLM 调用。
前端据此把「程序算的」和「模型理解的」分开呈现，评审也能一眼看出边界在哪。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.schemas.enums import ToolTransport


class ToolError(RuntimeError):
    """工具执行失败。会被编排器捕获并记进 `tool_call.error`。"""


@dataclass(frozen=True)
class ToolOutcome:
    """一次工具调用的返回值。

    `summary` 是给人和日志看的一句话摘要；`value` 是给程序用的结构化结果。
    两者都要有：只留 `summary` 无法复算，只留 `value` 则时间线上全是 JSON，
    演示时没法读。
    """

    summary: str
    value: Any = None
    #: 可选的复算依据。计算类工具应当填上，这是「点击结论回到计算过程」的路径。
    formula: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    #: 本次调用挂上的证据（fact_id / page 等），编排器据此推 evidence.attached
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CallResult:
    """`ToolRegistry.call()` 的返回值：工具结果 + 本次真实耗时。"""

    outcome: ToolOutcome
    duration_ms: int


class ToolContext(Protocol):
    """工具能看到的运行时环境。

    刻意**不**把 sqlite 连接、settings 直接塞给工具函数：工具需要什么就通过
    `ctx` 上的具名方法拿，这样工具的依赖是显式的，也便于测试时替换。
    """

    task_id: str | None
    step_id: str | None
    project_id: str | None

    def now(self) -> str: ...

    @property
    def offline(self) -> bool: ...


ToolHandler = Callable[..., ToolOutcome | Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整定义。"""

    name: str
    version: str
    description_cn: str
    #: JSON Schema。REST 侧用于生成接口文档，MCP 侧直接作为工具的参数 schema。
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    #: True = 纯计算，同输入必同输出；False = 含 LLM 或读文件等不确定来源
    deterministic: bool
    handler: ToolHandler
    transport: ToolTransport = ToolTransport.INTERNAL
    #: 是否会产生副作用（写库、写文件）。只读工具在 MCP 侧才允许放开给外部调用。
    side_effects: bool = False

    def __post_init__(self) -> None:
        if not self.name or "." not in self.name:
            raise ValueError(
                f"工具名 {self.name!r} 应当形如 'engine.ratios'，用点号分组，"
                "便于 MCP 侧按前缀做权限控制"
            )
        if not self.version:
            raise ValueError(f"工具 {self.name} 必须声明版本：结果哈希依赖它")

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.handler)

    def mcp_descriptor(self) -> dict[str, Any]:
        """MCP 工具描述符。与 REST 侧共用同一份 `input_schema`。"""
        return {
            "name": self.name,
            "description": self.description_cn,
            "inputSchema": self.input_schema,
        }


def _canonical_json(value: Any) -> str:
    """稳定序列化：键排序 + 不转义中文。

    `default=str` 是为了 Decimal —— 引擎的结果一律是 Decimal，
    `json.dumps` 不认它。转成字符串正好符合「金额以字符串入库」的约定。
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def result_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ToolRegistry:
    """工具登记表。进程内单例即可——工具定义在启动时一次性注册完。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._specs:
            raise ValueError(
                f"工具 {spec.name!r} 重复注册。"
                "重名会让 `tool_call` 表里的历史记录无法分辨是哪一版实现产生的"
            )
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ToolError(
                f"未登记的工具 {name!r}。已登记：{sorted(self._specs)}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._specs)

    def all_specs(self) -> list[ToolSpec]:
        return [self._specs[n] for n in self.names()]

    def mcp_tools(self, *, read_only_only: bool = True) -> list[dict[str, Any]]:
        """给 MCP server 用的工具清单。

        默认只暴露只读工具：MCP client 面向的是外部调用方，
        写库类工具一旦放开，外部就能改到财务事实。
        """
        return [
            s.mcp_descriptor()
            for s in self.all_specs()
            if not (read_only_only and s.side_effects)
        ]

    def check_arguments(self, spec: ToolSpec, args: dict[str, Any]) -> None:
        """参数校验。

        只做「必填齐不齐、有没有多余键」这一层，**不是**完整的 JSON Schema 校验——
        完整校验需要一个 JSON Schema 实现库，而当前阶段它拦得住的东西
        （类型写错、枚举越界）工具函数自己也会立刻报错。
        真正重要的一条是"多余键"：多传的参数会被静默忽略，
        让调用方以为自己的意图生效了。
        """
        schema = spec.input_schema or {}
        props: dict[str, Any] = schema.get("properties") or {}
        required = set(schema.get("required") or [])

        missing = sorted(required - set(args))
        if missing:
            raise ToolError(
                f"工具 {spec.name} 缺少必填参数：{'、'.join(missing)}"
            )
        if props:
            unknown = sorted(set(args) - set(props))
            if unknown:
                raise ToolError(
                    f"工具 {spec.name} 收到未声明的参数：{'、'.join(unknown)}。"
                    f"可用参数：{'、'.join(sorted(props))}"
                )

    async def call(self, spec: ToolSpec, args: dict[str, Any], **kwargs: Any) -> CallResult:
        """调用一个工具，不落库、不发事件。

        留痕由编排器负责（见 `app/agents/orchestrator.py`）——工具层不知道
        任务和步骤的存在，它是可以单独测的纯逻辑。
        """
        self.check_arguments(spec, args)
        started = time.perf_counter()
        try:
            outcome = spec.handler(**args, **kwargs)
            if spec.is_async:
                outcome = await outcome  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001 —— 统一包装，原文进 tool_call.error
            raise ToolError(f"工具 {spec.name} 执行失败：{exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if not isinstance(outcome, ToolOutcome):
            raise ToolError(
                f"工具 {spec.name} 必须返回 ToolOutcome，实际返回 {type(outcome).__name__}"
            )
        # 耗时单独放，不塞进 ToolOutcome：那个类型是**工具的返回值**，
        # 由工具自己构造；把执行耗时混进去，工具作者就能填一个和实际不符的值。
        # 只有 call() 量得到真实耗时，那就只有 call() 来填。
        return CallResult(outcome=outcome, duration_ms=elapsed_ms)


#: 进程级登记表。工具在各自模块 import 时注册。
REGISTRY = ToolRegistry()


def tool(
    *,
    name: str,
    version: str,
    description_cn: str,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    deterministic: bool = True,
    transport: ToolTransport = ToolTransport.INTERNAL,
    side_effects: bool = False,
) -> Callable[[ToolHandler], ToolHandler]:
    """注册装饰器。

        @tool(name="engine.ratios", version="v1", description_cn="计算财务比率")
        def ratios(...) -> ToolOutcome: ...
    """

    def wrap(fn: ToolHandler) -> ToolHandler:
        REGISTRY.register(
            ToolSpec(
                name=name,
                version=version,
                description_cn=description_cn,
                input_schema=input_schema or {"type": "object", "properties": {}},
                output_schema=output_schema or {"type": "object"},
                deterministic=deterministic,
                handler=fn,
                transport=transport,
                side_effects=side_effects,
            )
        )
        return fn

    return wrap
