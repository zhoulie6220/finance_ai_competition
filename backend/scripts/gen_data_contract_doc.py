"""重新生成 docs/01-data-contract.md 里的字段字典表格。

文档正文是手写的 markdown，只有两个标记之间的表格由本脚本生成：

    <!-- BEGIN GENERATED: metrics -->   ... <!-- END GENERATED: metrics -->

这样会计同学可以在文档里随手加说明，而表格永远和 `app/data/seed/0001_metric_definitions.sql`
保持一致。字段字典是 PDF 行名映射的唯一依据，一旦文档和种子数据分叉，解析结果就会
难以解释。

用法：
    python scripts/gen_data_contract_doc.py            # 就地更新
    python scripts/gen_data_contract_doc.py --check    # 只校验是否一致
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import DEFAULT_DB_PATH  # noqa: E402

REPO_DIR = Path(__file__).resolve().parent.parent.parent
DOC_PATH = REPO_DIR / "docs" / "01-data-contract.md"

BEGIN = "<!-- BEGIN GENERATED: metrics -->"
END = "<!-- END GENERATED: metrics -->"

# 按报表分组输出；（标题, statement 取值）
GROUPS: list[tuple[str, str]] = [
    ("利润表", "income"),
    ("资产负债表", "balance"),
    ("现金流量表", "cashflow"),
    ("股本与每股", "indicator"),
    ("披露事项", "disclosure"),
]

# 行业专属指标（industry 字段取值 → 标题）
INDUSTRY_GROUPS: list[tuple[str, str]] = [
    ("steel", "钢铁专属"),
    ("energy", "能源专属"),
]

_VALUE_TYPE_CN = {"flow": "期间", "stock": "时点", "ratio": "比率", "text": "文本"}

# 例句来源标记（example_source 取值 → 展示文案）
_EXAMPLE_SOURCE_CN = {
    "annual_report": "年报原文",
    "synthetic_example": "占位符句",
}


def render() -> str:
    con = sqlite3.connect(DEFAULT_DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT metric_key, label_cn, statement, value_type, unit_kind, is_nonrecurring,"
        " is_derived, industry, parent_key, aliases, exclusion_terms,"
        " example_sentence, example_source"
        " FROM metric_definition ORDER BY display_order"
    ).fetchall()
    con.close()

    def table(items: list[sqlite3.Row]) -> list[str]:
        out = [
            "| 字段键 | 标准名称 | 类型 | 单位 | 派生 | 非经常 | 年报行名别名 | 排除词 |",
            "|---|---|---|---|:--:|:--:|---|---|",
        ]
        for r in items:
            # 空列表既可能存成 NULL 也可能存成 '[]'（手写种子时两种都会出现），
            # 解析后再判断空不空，否则 '[]' 会渲染成空白格而不是「—」。
            aliases = "、".join(json.loads(r["aliases"] or "[]")) or "—"
            exclusions = "、".join(json.loads(r["exclusion_terms"] or "[]")) or "—"
            out.append(
                f"| `{r['metric_key']}` | {r['label_cn']}"
                f" | {_VALUE_TYPE_CN.get(r['value_type'], r['value_type'])}"
                f" | {r['unit_kind']}"
                f" | {'✓' if r['is_derived'] else ''}"
                f" | {'✓' if r['is_nonrecurring'] else ''}"
                f" | {aliases} | {exclusions} |"
            )
        return out

    lines: list[str] = []
    emitted: set[str] = set()

    def emit(title: str, items: list[sqlite3.Row]) -> None:
        if not items:
            return
        lines.extend([f"#### {title}（{len(items)} 项）", ""])
        lines.extend(table(items))
        lines.append("")
        emitted.update(r["metric_key"] for r in items)

    for title, statement in GROUPS:
        emit(title, [r for r in rows if r["statement"] == statement and not r["industry"]])

    for industry, title in INDUSTRY_GROUPS:
        emit(title, [r for r in rows if r["industry"] == industry])

    # 兜底分组：statement 与 industry 的取值组合不在上面任何一组里时（例如
    # statement='industry' 但 industry 为空），该字段会被**静默漏掉**——文档里
    # 少一行，谁也不会发现。宁可多一个「其他」分组，也不能让字段消失。
    emit("其他", [r for r in rows if r["metric_key"] not in emitted])

    total = len(emitted)
    assert total == len(rows), (
        f"有 {len(rows) - total} 个字段没有被任何分组收录："
        f"{[r['metric_key'] for r in rows if r['metric_key'] not in emitted]}"
    )
    lines.append(f"合计 **{total}** 个字段。")
    lines.append("")

    # 例句锚点单独成节：句子有几十字，塞进上面的宽表会把它撑到没法看。
    # 「占位符句」是带【数值】的标准句，只供解析器回归测试，**不得**当成年报原文展示。
    with_example = [r for r in rows if r["example_sentence"]]
    synthetic = sum(1 for r in with_example if r["example_source"] == "synthetic_example")
    lines += [
        f"#### 例句锚点（{len(with_example)}/{total} 项已填，其中占位符句 {synthetic} 项）",
        "",
        "| 字段键 | 例句 | 来源 |",
        "|---|---|---|",
    ]
    for r in with_example:
        src = _EXAMPLE_SOURCE_CN.get(r["example_source"] or "", "未标注")
        lines.append(f"| `{r['metric_key']}` | {r['example_sentence']} | {src} |")
    lines.append("")
    lines.append(
        f"> 仍有 **{total - len(with_example)}** 个字段没有例句锚点。"
        "占位符句必须在上传真实年报后逐条替换为原文，并把 `example_source` 改为"
        " `annual_report`——在此之前它不能作为任何结论的证据。"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成字段字典表格")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args()

    if not DOC_PATH.exists():
        print(f"找不到文档：{DOC_PATH}", file=sys.stderr)
        return 1

    doc = DOC_PATH.read_text(encoding="utf-8")
    if BEGIN not in doc or END not in doc:
        print(f"文档里找不到标记 {BEGIN} / {END}", file=sys.stderr)
        return 1

    head, rest = doc.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    updated = f"{head}{BEGIN}\n\n{render()}\n\n{END}{tail}"

    if args.check:
        if updated != doc:
            print("✗ 字段字典表格与种子数据不一致，请运行："
                  "python scripts/gen_data_contract_doc.py", file=sys.stderr)
            return 1
        print("✓ 字段字典表格与种子数据一致")
        return 0

    if updated == doc:
        print("✓ 无需更新")
        return 0

    DOC_PATH.write_text(updated, encoding="utf-8")
    print(f"已更新 {DOC_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
