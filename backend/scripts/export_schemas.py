"""把 Pydantic 契约导出为 JSON Schema，供前端生成 TypeScript 类型。

用法：
    python scripts/export_schemas.py            # 写入 frontend/src/types/
    python scripts/export_schemas.py --check    # 只校验是否与已导出的文件一致

为什么要这一步：前后端对同一个字段的理解一旦分叉（后端叫 value、前端渲染 amount），
返工成本远大于自动生成。契约变更后跑一次本脚本，`--check` 可以放进 CI 卡住漂移。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _console  # noqa: E402  (与本文件同目录)

_console.setup()

from pydantic import BaseModel  # noqa: E402

from app import schemas  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
OUT_DIR = REPO_DIR / "frontend" / "src" / "types"

#: 契约里的集合模型 → 导出文件名（去掉了 *List 包装，前端按名字 import 即可）
EXPORTED_MODELS = [
    "FinancialFact",
    "FactObservation",
    "FactCorrection",
    "CheckResult",
    "MetricDefinition",
    "MdnaSection",
    "Claim",
    "ClaimIndicator",
    "ClaimMatch",
    "DiagnosisRun",
    "DiagnosisComponent",
    "RuleConfigItem",
    "ScenarioDelta",
    "NormalizationRun",
    "NormalizationYear",
    "EbitAdjustment",
    "NormalizationCrosscheck",
    "ComparableCompany",
    "ValuationRun",
    "ValuationScenario",
    "ValuationParam",
    "Task",
    "TaskStep",
    "ToolCall",
    "LlmCall",
    "FileAccessLog",
    "RunManifest",
    "AppLog",
    "TaskEvent",
    "Evidence",
    "Report",
    "ReportCitation",
    "AuditFinding",
    # HTTP 接口形状。前端消费的就是这些——没有它们，丙 只能照着 /docs 手抄
    # 响应结构，而手抄的会在后端加字段时静默过期。
    "TaskSummary",
    "TaskStepView",
    "TaskResponse",
    "TaskListResponse",
    "ToolCallListResponse",
    "EventHistoryResponse",
    "ValidationIssue",
    "ValidationErrorResponse",
    "HealthResponse",
    "DatabaseStatus",
    "ToolSpecView",
    "ToolListResponse",
    "SkillView",
    "SkillListResponse",
    "MetaResponse",
    "ProjectView",
    "ProjectListResponse",
    "FileView",
    "FileListResponse",
]

#: 文件头写成 `$comment` 而不是 `/* */`。
#:
#: ⚠ 这里曾经写成 JS 风格的注释头，结果是**这份文件根本不是合法 JSON**：
#:   浏览器里 `import contract from './types/contract.json'` 会解析失败，
#:   `JSON.parse` 也一样。文件看起来很正常、`--check` 也一直是绿的
#:   （它比的是文本，不是能不能解析），但前端一个字都用不上它。
#:   `$comment` 是 JSON Schema 标准字段，两边都不耽误。
HEADER_COMMENT = (
    "本文件由 backend/scripts/export_schemas.py 自动生成，请勿手改。\n"
    "数据契约的唯一定义源在 backend/app/schemas/（Pydantic v2），\n"
    "人类可读版本见 docs/01-data-contract.md。\n"
    "契约变更后请重新运行：cd backend && python scripts/export_schemas.py"
)

TS_HEADER = """/* 本文件由 backend/scripts/export_schemas.py 自动生成，请勿手改。
 *
 * 数据契约的唯一定义源在 backend/app/schemas/（Pydantic v2）。
 * 契约变更后请重新运行：
 *     cd backend && python scripts/export_schemas.py
 *
 * 枚举导出成**字面量联合类型**而不是 TS `enum`：
 * 一是 `tsconfig.app.json` 开了 `erasableSyntaxOnly`，`enum` 会被编译拒绝；
 * 二是联合类型在收窄和穷尽检查上更好用，而且和 JSON 里的取值是同一批字符串。
 */

"""


def build_schema() -> dict:
    """生成一份合并的 JSON Schema。

    用 $defs 集中存放全部模型，模型之间用 $ref 互引，避免同一类型重复定义
    （重复定义是前后端类型漂移的常见来源）。

    ⚠ 用 `mode="serialization"`：默认的 `mode="validation"` 会把
      `Field(exclude=True)` 的字段也列进 properties，于是契约里写着「有这个字段」，
      接口却从来不返回它——前端照着写 `task.steps` 拿到 `undefined`，不报错。
      `TaskSummary.steps` 正是这种情况。
    """
    defs: dict[str, dict] = {}
    for name in EXPORTED_MODELS:
        model = getattr(schemas, name, None)
        if model is None:
            raise SystemExit(f"schemas 里找不到模型：{name}")
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise SystemExit(f"{name} 不是 Pydantic 模型")
        defs[name] = model.model_json_schema(
            mode="serialization", ref_template="#/$defs/{model}"
        )

    # 把各模型自己那份 $defs 里的条目平铺到顶层，$ref 路径统一改指 #/$defs/
    merged_defs: dict[str, dict] = {}
    for name, sch in defs.items():
        for sub_name, sub in (sch.get("$defs") or {}).items():
            merged_defs.setdefault(sub_name, sub)
        sch.pop("$defs", None)
        merged_defs[name] = sch

    for sch in merged_defs.values():
        _rewrite_refs(sch)

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": HEADER_COMMENT,
        "title": "投研工作台数据契约",
        "description": "由 backend/app/schemas/ 生成",
        "$defs": merged_defs,
    }


# --------------------------------------------------------------- TypeScript 生成

_TS_SCALARS = {
    "string": "string", "integer": "number", "number": "number",
    "boolean": "boolean", "null": "null",
}
_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _jsdoc(text: str, indent: str) -> str:
    """字段说明 → JSDoc。注释里出现 `*/` 会提前结束注释，必须打散。"""
    clean = text.replace("*/", "*​/").strip()
    if not clean:
        return ""
    lines = clean.splitlines()
    if len(lines) == 1:
        return f"{indent}/** {lines[0]} */\n"
    body = "".join(f"{indent} * {ln}\n" for ln in lines)
    return f"{indent}/**\n{body}{indent} */\n"


def _ts_type(sch: dict) -> str:
    """JSON Schema 片段 → TypeScript 类型。只处理本项目实际会出现的形状。"""
    if "$ref" in sch:
        return sch["$ref"].rsplit("/", 1)[-1]
    # ⚠ enum 必须排在 type 前面：枚举 schema 同时带 `"type": "string"`，
    #   先看 type 的话所有枚举都会退化成 `string`——**收窄就全废了**，
    #   前端写 status === 'succeed' 不再报错，而这正是这套类型的全部价值。
    if "enum" in sch:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in sch["enum"])
    if "anyOf" in sch:
        parts: list[str] = []
        for sub in sch["anyOf"]:
            t = _ts_type(sub)
            if t not in parts:  # Pydantic 有时会把同一个类型拆开重列
                parts.append(t)
        # 全可选且含 null → 直接联合；否则加括号避免和数组 [] 打架
        return " | ".join(parts)
    t = sch.get("type")
    if t == "array":
        inner = _ts_type(sch.get("items") or {})
        return f"({inner})[]" if "|" in inner else f"{inner}[]"
    if t == "object":
        ap = sch.get("additionalProperties")
        if isinstance(ap, dict):
            return f"Record<string, {_ts_type(ap)}>"
        return "Record<string, unknown>"
    return _TS_SCALARS.get(t or "", "unknown")


def _render_def(name: str, sch: dict) -> str:
    doc = _jsdoc(sch.get("description", ""), "")
    if "enum" in sch:
        # 字面量联合：tsconfig 的 erasableSyntaxOnly 禁 enum，这样也更好收窄
        return f"{doc}export type {name} = {_ts_type(sch)};\n"

    if sch.get("type") == "object" and "properties" in sch:
        required = set(sch.get("required") or [])
        lines = [f"{doc}export interface {name} {{\n"]
        for field, sub in sch["properties"].items():
            key = field if _IDENT.match(field) else json.dumps(field)
            opt = "" if field in required else "?"
            lines.append(_jsdoc(sub.get("description", ""), "  "))
            lines.append(f"  {key}{opt}: {_ts_type(sub)};\n")
        if isinstance(sch.get("additionalProperties"), dict):
            lines.append(f"  [key: string]: {_ts_type(sch['additionalProperties'])};\n")
        lines.append("}\n")
        return "".join(lines)

    return f"{doc}export type {name} = {_ts_type(sch)};\n"


def render_event_types() -> str:
    """把 SSE 事件类型导出去。

    前端**必须**有这份清单：SSE 里 `event: step.started` 是具名事件，
    不会触发 `onmessage`，必须 `addEventListener('step.started', ...)` 逐个注册。
    清单写死在浏览器里的话，后端加一个新事件类型，前端那一格就**静默不显示**——
    时间线上少一格，看起来和「这一步本来就没有」一模一样。

    与 `state.py::EVENT_TYPES` 同源，改了那边这里自动跟上。
    """
    from app.agents.state import EVENT_TYPES

    items = ",\n  ".join(f'"{t}"' for t in sorted(EVENT_TYPES))
    return (
        "/**\n"
        " * SSE 事件类型白名单。与 backend/app/agents/state.py::EVENT_TYPES 同源。\n"
        " *\n"
        " * 服务端发的是**具名事件**（`event: step.started`），不会触发 EventSource\n"
        " * 的 onmessage，必须按类型逐个 addEventListener。所以前端需要这份清单——\n"
        " * 也就意味着它不能靠手抄：漏一个类型的后果是那一格**静默不显示**。\n"
        " */\n"
        f"export const EVENT_TYPES = [\n  {items},\n] as const;\n\n"
        "export type TaskEventType = (typeof EVENT_TYPES)[number];\n\n"
        "/** 收到这几个事件就意味着任务结束了，流会关掉。 */\n"
        'export const TASK_ENDING_EVENTS: readonly TaskEventType[] = ["task.completed", "task.failed"];\n\n'
    )


def render_typescript(schema: dict) -> str:
    parts = [TS_HEADER, render_event_types()]
    for name in sorted(schema["$defs"]):
        parts.append(_render_def(name, schema["$defs"][name]))
        parts.append("\n")
    return "".join(parts)


def _rewrite_refs(node: object) -> None:
    """把局部 #/$defs/X 统一成顶层 #/$defs/X。"""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            node["$ref"] = "#/$defs/" + ref[len("#/$defs/"):]
        for v in node.values():
            _rewrite_refs(v)
    elif isinstance(node, list):
        for v in node:
            _rewrite_refs(v)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出前端类型")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args()

    schema = build_schema()
    outputs = {
        OUT_DIR / "contract.json":
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        OUT_DIR / "contract.ts": render_typescript(schema),
    }

    if args.check:
        stale = [p for p, text in outputs.items()
                 if not p.exists() or p.read_text(encoding="utf-8") != text]
        if stale:
            for p in stale:
                print(f"✗ 与契约不一致：{p}", file=sys.stderr)
            print("  重新生成：python scripts/export_schemas.py", file=sys.stderr)
            return 1
        print("✓ 契约与已导出的类型一致（contract.json / contract.ts）")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")

    names = sorted(schema["$defs"])
    print(f"已导出到 {OUT_DIR}")
    print(f"  模型 {len(EXPORTED_MODELS)} 个，含依赖共 {len(names)} 个类型定义")
    for path in outputs:
        print(f"  · {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
