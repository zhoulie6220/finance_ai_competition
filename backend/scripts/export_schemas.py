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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
]

HEADER = """/* 本文件由 backend/scripts/export_schemas.py 自动生成，请勿手改。
 *
 * 数据契约的唯一定义源在 backend/app/schemas/（Pydantic v2），
 * 人类可读版本见 docs/01-data-contract.md。
 * 契约变更后请重新运行：
 *     cd backend && python scripts/export_schemas.py
 */
"""


def build_schema() -> dict:
    """生成一份合并的 JSON Schema。

    用 $defs 集中存放全部模型，模型之间用 $ref 互引，避免同一类型重复定义
    （重复定义是前后端类型漂移的常见来源）。
    """
    defs: dict[str, dict] = {}
    for name in EXPORTED_MODELS:
        model = getattr(schemas, name, None)
        if model is None:
            raise SystemExit(f"schemas 里找不到模型：{name}")
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise SystemExit(f"{name} 不是 Pydantic 模型")
        defs[name] = model.model_json_schema(ref_template="#/$defs/{model}")

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
        "title": "投研工作台数据契约",
        "description": "由 backend/app/schemas/ 生成",
        "$defs": merged_defs,
    }


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

    content = HEADER + json.dumps(build_schema(), ensure_ascii=False, indent=2) + "\n"
    target = OUT_DIR / "contract.json"

    if args.check:
        if not target.exists():
            print(f"✗ 尚未导出：{target}", file=sys.stderr)
            return 1
        if target.read_text(encoding="utf-8") != content:
            print("✗ 契约已变更，需重新运行 python scripts/export_schemas.py", file=sys.stderr)
            return 1
        print("✓ 契约与已导出的类型一致")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    names = sorted(build_schema()["$defs"])
    print(f"已导出 {target}")
    print(f"  模型 {len(EXPORTED_MODELS)} 个，含依赖共 {len(names)} 个类型定义")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
