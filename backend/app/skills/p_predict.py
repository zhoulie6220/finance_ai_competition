"""用模型给 P 表做**预判**——只出候选，不定分。

## 边界划在哪（这一段是这张模块存在的理由）

会计口径原话：**「模型只能提出候选，不能自动定 P」**。所以：

  · 产出落 `p_prediction`，**不是** `p_confirmation`。两张表不相通。
  · `p_prediction` **没有 conclusion 列**——模型碰不到分数。
    它只答三个描述性问题：是不是实质表述、是不是套话、缺哪些要素。
  · P 的分子分母只认 `p_confirmation`，也就是**只认会计签了字的那一份**。

## 它解决什么

P 表 185 行，逐条从零填对会计是实打实的负担，而**拖着不填**的结果是指数
一直出不了分。预判把它变成「复核」：值已经在那儿，会计改他不认同的。

## ⚠ 它带来的风险，以及本模块的应对

**锚定**：人看到已有答案，容易直接接受。§4.4 要的是「逐条人工确认」，
如果退化成橡皮图章，P 这个分项的正当性就没了——答辩时被问「这 185 条谁判的」，
答「模型判的」不好听。

应对有三条，都落在别处，这里记一笔免得被改掉：
  1. 导出的 CSV 里有独立的 `预判` 标记列，一眼看得出哪些值不是人填的
  2. 导出时带 `预判理由` 列——只给标签不给依据，复核就退化成「看着顺眼就点头」
  3. `conclusion` 一律留空，**模型永远不填那一列**
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.llm.client import LlmClient, SchemaError
from app.agents.llm.prompts.registry import PromptRegistry
from app.agents.llm.settings import LlmSettings
from app.agents.llm.transport import build_transport
from app.skills import claim_scope
from app.skills.llm_log import make_sink

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROMPTS_DIR = BACKEND_DIR / "app" / "agents" / "llm" / "prompts"

PROMPT_KEY = "claim_confirm"

#: 一次问几句。提示词里要求「一条不能少」，句子太多模型会漏——
#: 实测 8 句一组时没有漏判。调大之前先看 `claims_predicted` 对不对得上。
BATCH_SIZE = 8

#: `missing_elements` 的合法取值，与会计口径 §4.2 一致
ALLOWED_ELEMENTS = ("object", "period", "metric", "result", "owner")


@dataclass
class PredictSummary:
    project_id: str
    claims_considered: int = 0
    claims_predicted: int = 0
    claims_skipped_existing: int = 0
    calls: int = 0
    tokens: int = 0
    batches_failed: int = 0
    #: 模型没回填到的句子数。**不为 0 就要看**——它会静默留白，
    #: 会计看到的是「这行没预判」，看不出是模型漏了还是本来就不该有。
    not_returned: int = 0
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"候选 {self.claims_considered} 条",
            f"预判 {self.claims_predicted} 条",
        ]
        if self.claims_skipped_existing:
            parts.append(f"已有预判跳过 {self.claims_skipped_existing} 条")
        if self.not_returned:
            parts.append(f"⚠ 模型未回填 {self.not_returned} 条")
        parts.append(f"{self.calls} 次调用 / 约 {self.tokens / 1000:.0f}K tokens")
        return "；".join(parts)


def predict_for_project(
    con: sqlite3.Connection,
    project_id: str,
    *,
    now: str,
    client: LlmClient | None = None,
    limit: int | None = None,
) -> PredictSummary:
    """给一个项目的 P 候选做预判。幂等：已有预判的条目会跳过。"""
    summary = PredictSummary(project_id=project_id)

    if client is None:
        settings = LlmSettings.from_env()
        if not settings.offline_mode and not settings.configured:
            summary.warnings.append(
                "没配模型密钥，也未开离线回放。填 backend/.env 的 LLM_API_KEY，"
                "或把 OFFLINE_MODE 置 1 走预录回放。"
            )
            return summary
        client = LlmClient(
            settings=settings,
            transport=build_transport(
                offline_mode=settings.offline_mode,
                base_url=settings.base_url,
                api_key=settings.api_key,
                cassette_dir=settings.cassette_dir,
                record=not settings.offline_mode,
            ),
            prompts=PromptRegistry(PROMPTS_DIR),
            sink=make_sink(con),
        )
    settings = client.settings

    pending = _pending_claims(con, project_id)
    summary.claims_skipped_existing = _existing_count(con, project_id)
    if limit is not None:
        pending = pending[:limit]
    summary.claims_considered = len(pending)
    if not pending:
        return summary

    rows: list[tuple[Any, ...]] = []
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start : start + BATCH_SIZE]
        result = client.complete_json(
            purpose="claim_confirm",
            prompt_key=PROMPT_KEY,
            variables={"claims": _render_batch(batch)},
            validator=_validate_shape,
        )
        summary.calls += result.call_count
        for attempt in result.attempts:
            summary.tokens += (attempt.tokens_in or 0) + (attempt.tokens_out or 0)

        if not result.ok:
            summary.batches_failed += 1
            summary.warnings.append(
                f"第 {start // BATCH_SIZE + 1} 批失败："
                f"{(result.attempts[-1].error or '')[:80]}"
            )
            continue

        by_id = {c["claim_id"]: c for c in batch}
        returned: set[str] = set()
        for j in (result.data or {}).get("judgements") or []:
            if not isinstance(j, dict):
                continue
            claim_id = str(j.get("claim_id") or "").strip()
            # ⚠ 模型回填的 id 必须在**这一批**里。它偶尔会串批或者编一个，
            # 不挡住的话，A 句的预判会写到 B 句头上，而两行看起来都正常。
            if claim_id not in by_id or claim_id in returned:
                continue
            returned.add(claim_id)
            rows.append(_build_row(claim_id, j, result, settings.model, now))
        summary.not_returned += len(batch) - len(returned)

    if rows:
        with con:
            con.executemany(
                "INSERT OR REPLACE INTO p_prediction (id, claim_id, is_substantive,"
                " is_template, missing_elements, reason, model, prompt_version,"
                " prompt_hash, llm_call_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        summary.claims_predicted = len(rows)

    return summary


# ---------------------------------------------------------------- 内部


def _pending_claims(
    con: sqlite3.Connection, project_id: str
) -> list[dict[str, Any]]:
    """该项目下**还没有预判**的 P 候选。

    ⚠ 候选来源必须与导出、判定、指数走**同一个开关**（claim_scope）。
    各写各的 WHERE，预判就会覆盖到一批「不进指数」的句子，
    或者漏掉一批「进指数」的——**两边都不报错**。
    """
    scope, params = claim_scope.scope_sql("c")
    rows = con.execute(
        f"""
        SELECT c.claim_id, c.claim_text, c.source_page
        FROM claim c
        WHERE c.project_id = ? AND c.verifiable = 1{scope}
          AND NOT EXISTS (SELECT 1 FROM p_prediction p WHERE p.claim_id = c.claim_id)
        ORDER BY c.claim_type, c.source_page
        """,
        (project_id, *params),
    ).fetchall()
    return [dict(r) for r in rows]


def _existing_count(con: sqlite3.Connection, project_id: str) -> int:
    scope, params = claim_scope.scope_sql("c")
    return con.execute(
        f"""
        SELECT COUNT(*) FROM p_prediction p
        JOIN claim c ON c.claim_id = p.claim_id
        WHERE c.project_id = ?{scope}
        """,
        (project_id, *params),
    ).fetchone()[0]


def _render_batch(batch: list[dict[str, Any]]) -> str:
    """把一批句子渲染进提示词。

    带 claim_id 是为了让模型**原样回填**——按序号回填的话，
    模型少数一句、后面全体错位，而错位的预判看起来和正确的没差别。
    """
    lines = []
    for c in batch:
        text = " ".join((c["claim_text"] or "").split())
        lines.append(f"- claim_id={c['claim_id']}\n  原文：{text}")
    return "\n".join(lines)


def _validate_shape(data: dict) -> None:
    """结构不对就抛，触发 schema_repair 重试。

    取值不认识**不抛**——那在下面归一到缺省值，比让整批重试划算。
    但结构不对（judgements 不是数组）必须抛，否则下游拿到一个
    「长得像但用不了」的 dict。
    """
    judgements = data.get("judgements")
    if not isinstance(judgements, list):
        raise SchemaError(
            f"judgements 必须是数组，收到 {type(judgements).__name__}"
        )
    for i, j in enumerate(judgements):
        if not isinstance(j, dict):
            raise SchemaError(f"judgements[{i}] 不是对象")
        if not j.get("claim_id"):
            raise SchemaError(f"judgements[{i}] 缺 claim_id")


def _as_bool_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)) and value in (0, 1):
        return int(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "是", "yes"):
            return 1
        if low in ("false", "0", "否", "no"):
            return 0
    return None


def _build_row(
    claim_id: str,
    j: dict[str, Any],
    result: Any,
    model: str,
    now: str,
) -> tuple[Any, ...]:
    substantive = _as_bool_int(j.get("is_substantive"))
    if substantive is None:
        # 判不出来时按「是实质表述」处理：那会让它**进分母**，
        # 也就是让会计必须看一眼——比默认排除、悄悄少算一条要好。
        substantive = 1
    elements = [
        e for e in (j.get("missing_elements") or []) if e in ALLOWED_ELEMENTS
    ]
    return (
        f"pp-{claim_id}",
        claim_id,
        substantive,
        _as_bool_int(j.get("is_template")),
        json.dumps(elements, ensure_ascii=False) if elements else None,
        (str(j.get("reason") or "").strip() or None),
        model,
        result.prompt_version,
        result.prompt_hash,
        result.call_id,
        now,
    )


def prompt_sha256() -> str:
    """当前 prompt 的内容哈希。给脚本打印用，方便确认用的是哪一版。"""
    p = PROMPTS_DIR / f"{PROMPT_KEY}.v1.md"
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.exists() else "?"
