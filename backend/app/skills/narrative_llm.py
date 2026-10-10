"""LLM 版主张抽取——叠在规则法基线上。

甲阶段二原话：「LLM 版主张抽取（**在规则法基线上叠，不是从零写**）」。

基线（`app/skills/narrative.py`）先铺好了三件事：
主题规则表、期间解析、以及一套确定性的 `claim_id` 生成方式。
这一层复用它们，只把「哪些句子算主张」这一步换成模型来判。

## 两者并存，不是替换

规则法的 `claim_id` 前缀是 `cl-`，LLM 版是 `cl-llm-`——**同一句话两边都抽到时
会存成两条**，靠 `extractor` 区分：

    rule:claim_v1                    规则法
    llm:deepseek-chat@<prompt_hash>  模型抽取

这是刻意的：验收标准要求「能和规则法对照」，而**替换掉就对照不起来了**。
「哪个更准」这件事必须由人看着两边的结果来判，不能由代码预先裁定。

## 一道防线：模型写的「原句」必须真的在原文里

`claim_text` 是证据链的终点——它必须能在年报里被找到。所以每一句都回原文
核一遍（去掉空白后比对）：**找不到的标 `needs_review` 并写明原因**，
不静默收下。

这是数字守卫在文本层面的对应物：数字守卫拦编造的**数字**，
这一道拦编造的**句子**。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app.agents.llm.client import LlmClient, SchemaError
from app.agents.llm.prompts.registry import PromptRegistry
from app.agents.llm.settings import LlmSettings
from app.agents.llm.transport import build_transport
from app.db import dictionary
# ★ **复用规则法基线**。方案原话是「LLM 版主张抽取（在规则法基线上叠，
# 不是从零写）」——期间归一化就是基线上现成的东西，不该再写一遍。
# 自己重写一版的话，两边的期间口径会慢慢分叉，而分叉不会报错，
# 只会让「同一个主张在两种抽取法下指向不同的年度」。
from app.engine.claim_rules import (
    THEMES,
    extract_magnitude,
    extract_period_expr,
    resolve_period,
)
from app.parsing.claims import looks_like_table_row
from app.skills.llm_log import make_sink

#: claim_type → 该主题的主判据。**从规则层的主题表反查**，不另写一份。
#: 两边各写一份的话，改了一边另一边还是旧的，而错配不会报错——
#: 只会让一批主张悄无声息地落进「无主判据」、不参与任何判定。
_THEME_PRIMARY: dict[str, str | None] = {
    t.claim_type: t.primary_metric for t in THEMES if t.primary_metric
}

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROMPTS_DIR = BACKEND_DIR / "app" / "agents" / "llm" / "prompts"

PROMPT_KEY = "claim_extract"

#: 模型能返回的 claim_type。**必须与 schema.sql 的 CHECK 一致**——
#: 对不上时 INSERT 会被拒，而且拒绝发生在整批写入的中途。
#: 有测试盯着这个列表与提示词、数据库三方同源。
#:
#: ⚠ **这个集合不在那条测试的覆盖里**（测试比的是 schema ↔ 提示词）。
#: 漏加一个值的后果是 `normalize_claim_type` 把它**静默**归一成 `other`——
#: 主张照样落库、照样进判定，只是主题语义丢了，页面上看不出来。
ALLOWED_CLAIM_TYPES = {
    "demand", "order", "capacity", "collection",
    "product_mix", "cost", "risk", "macro", "other",
    "management_budget",
}

#: 模型返回的 direction，必须与 `claim.direction` 的 CHECK 一致
ALLOWED_DIRECTIONS = {"up", "down", "improve", "deteriorate", "flat", "unknown"}


@dataclass
class LlmExtractionSummary:
    project_id: str
    sections_attempted: int = 0
    sections_failed: int = 0
    claims_returned: int = 0
    claims_inserted: int = 0
    claims_skipped_existing: int = 0
    #: 模型写的「原句」在原文里找不到的条数 → 落 needs_review
    not_in_source: int = 0
    #: 模型把表格行当成主张抽出来的条数 → 直接丢弃（规则层有同样的剔除器）
    table_rows: int = 0
    #: 既没给有效主判据、主题表也兜不住，因而**不参与判定**的条数
    no_metric: int = 0
    calls: int = 0
    tokens: int = 0
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"扫描 {self.sections_attempted} 段",
            f"模型返回 {self.claims_returned} 条",
            f"入库 {self.claims_inserted} 条",
        ]
        if self.claims_skipped_existing:
            parts.append(f"已存在 {self.claims_skipped_existing} 条")
        if self.table_rows:
            parts.append(f"丢掉了 {self.table_rows} 条表格行（不是主张）")
        if self.no_metric:
            parts.append(f"无主判据 {self.no_metric} 条（不参与判定）")
        if self.not_in_source:
            parts.append(f"**原句在原文里找不到 {self.not_in_source} 条**")
        if self.sections_failed:
            parts.append(f"失败 {self.sections_failed} 段")
        parts.append(f"调用 {self.calls} 次 / {self.tokens} tokens")
        return "；".join(parts)


def extract_claims_llm(
    con: sqlite3.Connection,
    project_id: str,
    *,
    now: str | None = None,
    limit: int | None = None,
    section_ids: tuple[str, ...] | None = None,
    only_missing: bool = False,
    client: LlmClient | None = None,
) -> LlmExtractionSummary:
    """用模型抽主张。

    `limit` 用来先跑一小批看看效果——**399 段全跑一遍是 399 次调用**，
    每次两秒左右、都要花钱。先跑几段确认 prompt 和解析都对，再全量。

    `client` 只在测试里传：注入一个挂了 `FakeTransport` 的客户端，
    就能把**表格剔除、期间归一化、id 去重**这些逻辑测到，
    既不联网也不花钱。这条链路的逻辑值得单独测，而它跟「能不能连上
    DeepSeek」是两回事。

    ⚠ 已有的同 id 主张不会被覆盖（`INSERT OR IGNORE`）——
    重跑幂等，靠的是确定性 `claim_id`。
    """
    summary = LlmExtractionSummary(project_id=project_id)
    stamp = now or datetime.now(timezone.utc).isoformat(timespec="seconds")

    if client is None:
        settings = LlmSettings.from_env()
        if not settings.offline_mode and not settings.configured:
            summary.warnings.append(
                "没配模型密钥，也未开离线回放。填 backend/.env 的 LLM_API_KEY，"
                "或把 OFFLINE_MODE 置 1 走预录回放。"
            )
            return summary
        prompt = PromptRegistry(PROMPTS_DIR).get(PROMPT_KEY)
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
    else:
        settings = client.settings
        prompt = client.prompts.get(PROMPT_KEY)

    metric_keys = _metric_key_cheatsheet(con)

    sections = _load_sections(
        con, project_id, limit=limit, section_ids=section_ids,
        only_missing=only_missing,
    )
    rows: list[tuple[Any, ...]] = []
    indicators: list[tuple[Any, ...]] = []
    valid_metrics = _valid_metric_keys(con)
    # 主判据别名，用来在一句多目标时挑对那个数。与规则法取自同一份字段字典。
    aliases_by_metric = dictionary.metric_aliases(con)
    summary.no_metric = 0

    for section in sections:
        summary.sections_attempted += 1
        result = client.complete_json(
            purpose="claim_extract",
            prompt_key=PROMPT_KEY,
            variables={
                "metric_keys": metric_keys,
                "period": section["period"] or "",
                "section_heading": section["heading"] or "",
                "section_text": _clip(section["text"] or ""),
            },
            validator=_validate_shape,
        )
        summary.calls += result.call_count
        for attempt in result.attempts:
            summary.tokens += (attempt.tokens_in or 0) + (attempt.tokens_out or 0)

        if not result.ok:
            summary.sections_failed += 1
            summary.warnings.append(
                f"章节「{(section['heading'] or '')[:24]}」抽取失败："
                f"{(result.attempts[-1].error or '')[:80]}"
            )
            continue

        pages = _section_pages(con, section)
        for raw in (result.data or {}).get("claims", []):
            summary.claims_returned += 1
            text = str(raw.get("text") or "").strip()
            if not text:
                continue

            # ★ **复用规则法基线上的表格剔除器。**
            #
            # 实测：模型会把财务摘要表的行当成主张抽出来——
            #
            #     营业成本  149,258  168,931  -11.65
            #     财务费用  2,393  488  390.57
            #
            # 它们不是管理层说的话，是报表里的数字行。放进 claim 表会让 N 虚高、
            # 判定全是胡话，而且**看起来完全合理**——「成本下降了 11.65%」
            # 读起来就像一条真主张。
            #
            # 规则层的 `looks_like_table_row` 早就解决了这件事（靠 PDF 的列对齐，
            # 不是数字占比）。这一层**直接用它**，不重写——
            # 这正是「叠在规则法基线上」的字面意思。
            if looks_like_table_row(text):
                summary.table_rows += 1
                continue

            page_no = _locate_page(pages, text, section["page_from"])
            in_source = page_no is not None
            if not in_source:
                summary.not_in_source += 1

            # ★ **归一化只做一次，算完传下去。**
            #
            # 同一段归一化逻辑现在有三个用处：算 claim_id、选主判据、落库。
            # 三处各写一遍的话，它们会慢慢分叉——而分叉的后果是
            # 「id 按 A 算、落库按 B 存」，同一个模型输出在重跑时算出不同的 id，
            # 于是幂等失效、重复入库，**且不报错**。
            claim_type = normalize_claim_type(raw.get("claim_type"))
            metric = normalize_metric(
                raw.get("metric_key"), claim_type, valid_metrics
            )

            claim_id = _claim_id(section["section_id"], text, claim_type, metric or "")
            rows.append(
                _build_row(
                    claim_id=claim_id,
                    claim_type=claim_type,
                    section=section,
                    project_id=project_id,
                    report_period=section["period"] or "",
                    raw=raw,
                    text=text,
                    page_no=page_no if in_source else (section["page_from"] or 1),
                    in_source=in_source,
                    prompt_version=prompt.version,
                    extractor=f"llm:{settings.model}@{prompt.sha256[:8]}",
                    now=stamp,
                    metric_aliases=aliases_by_metric.get(metric or "", ()),
                )
            )

            # ★ **主判据必须写进 `claim_indicator`。**
            #
            # 第一版漏了这一步——结果 770 条 LLM 主张**一条都没参与判定**：
            # 匹配时读的是 `claim_indicator`，读不到就跳过，
            # 于是它们全落进「无主判据」那一堆而**不报错**。
            # 页面上看起来只是「模型抽了很多但不能判」，实际是接线断了。
            if not metric:
                summary.no_metric += 1
                continue
            indicators.append(
                (
                    f"ci-{claim_id}",
                    claim_id,
                    metric,
                    "primary",
                    max(0.0, min(1.0, confidence_of(raw))),
                    f"llm:{settings.model}@{prompt.sha256[:8]}",
                )
            )

    if rows:
        with con:
            before = con.execute(
                "SELECT COUNT(*) FROM claim WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
            con.executemany(
                "INSERT OR IGNORE INTO claim (claim_id, project_id, section_id,"
                " claim_text, subject, action, object, period_expr, period_norm,"
                " direction, magnitude_text, magnitude_value, magnitude_unit,"
                " magnitude_bound, is_plan_target, magnitude_metric_aligned,"
                " claim_type, verifiable, background_only, confidence,"
                " source_file_id, source_page, source_text, extractor,"
                " prompt_version, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            after = con.execute(
                "SELECT COUNT(*) FROM claim WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
            con.executemany(
                "INSERT OR IGNORE INTO claim_indicator (id, claim_id, metric_key,"
                " role, match_confidence, matched_by) VALUES (?,?,?,?,?,?)",
                indicators,
            )
        summary.claims_inserted = after - before
        summary.claims_skipped_existing = len(rows) - summary.claims_inserted

    if not sections:
        summary.warnings.append("没有可抽取的章节。先确认数据包已导入。")

    return summary


# ---------------------------------------------------------------- 内部


def normalize_claim_type(raw: object) -> str:
    """把模型给的主题归一到数据库允许的取值。

    不认识就归一成 `other`，**不丢这条**——内容可能是有价值的，
    只是主题分类没对上。丢掉的话，模型偶尔造一个新词就会静默少一条主张。
    """
    value = str(raw or "other")
    return value if value in ALLOWED_CLAIM_TYPES else "other"


def normalize_metric(
    raw: object, claim_type: str, valid_metrics: set[str]
) -> str | None:
    """把模型给的主判据归一到字段字典里真实存在的键。

    优先用模型选的（它读懂了那句话，比查表准）；
    模型没给、或给了字典外的键时，**退回主题表的主判据**——
    那条映射只有规则层那一份，反查即可，不另写。

    返回 None 表示这一条**没有可用的主判据**，不参与判定。
    """
    value = str(raw or "")
    if value in valid_metrics:
        return value
    return _THEME_PRIMARY.get(claim_type) or None


def _valid_metric_keys(con: sqlite3.Connection) -> set[str]:
    """字段字典里真实存在的 metric_key。

    `claim_indicator.metric_key` 有外键指向 `metric_definition`——
    模型自造一个键名就会违反外键，而拒绝发生在**整批写入的中途**。
    """
    return {r[0] for r in con.execute("SELECT metric_key FROM metric_definition")}


def confidence_of(raw: dict) -> float:
    try:
        return float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _validate_shape(data: dict) -> None:
    """校验结构；不通过抛 SchemaError，触发 schema_repair 重试。

    ⚠ **只在「结构不对」时抛**。取值不认识（比如模型给了个没见过的 claim_type）
    不抛——那会在代码里被归一到 `other`，比让整段重试划算。
    但结构不对（claims 不是数组、缺字段）必须抛，否则下游拿到一个
    「长得像但用不了」的 dict。
    """
    claims = data.get("claims")
    if not isinstance(claims, list):
        raise SchemaError(f"claims 必须是数组，收到 {type(claims).__name__}")
    for i, c in enumerate(claims):
        if not isinstance(c, dict):
            raise SchemaError(f"claims[{i}] 不是对象")
        if "text" not in c:
            raise SchemaError(f"claims[{i}] 缺 text 字段")


def _metric_key_cheatsheet(con: sqlite3.Connection) -> str:
    """给模型的字段表。

    只给**有中文标签**的字段，并且带上别名里最常见的一两个——
    模型靠中文名选键，不给中文它只能瞎猜。
    """
    rows = dictionary.fetch(con)
    lines = [f"{r['metric_key']}（{r['label_cn']}）" for r in rows if r.get("label_cn")]
    return "\n".join(lines)


def _clip(text: str, limit: int = 6000) -> str:
    """章节正文可能很长，截断。

    6000 字大约 4000 token，一次调用几厘钱。**截断要留痕**——
    悄悄截掉的话，后半段的主张永远抽不出来而没人知道。
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n（本段后面还有 {len(text) - limit} 字未展示）"


def _load_sections(
    con: sqlite3.Connection,
    project_id: str,
    *,
    limit: int | None,
    section_ids: tuple[str, ...] | None,
    only_missing: bool,
) -> list[Any]:
    """取要抽的章节。

    ⚠ `only_missing` 是给批量跑用的：`limit` 取的是「前 N 段」，
    而**不是「前 N 段还没抽过的」**。分批跑时如果不排除已抽过的，
    每一批都会从头再来一遍——数据不会错（`INSERT OR IGNORE` 挡住了），
    但**白花一遍调用的钱**，而且看起来「跑了很多批却没进展」。

    判据是「这一章的章节下有没有 LLM 抽出来的主张」。
    """
    clause = ""
    params: list[Any] = [project_id]
    if section_ids:
        clause = f" AND m.section_id IN ({','.join('?' * len(section_ids))})"
        params.extend(section_ids)
    if only_missing:
        clause += (
            " AND NOT EXISTS (SELECT 1 FROM claim c"
            " WHERE c.section_id = m.section_id AND c.extractor LIKE 'llm:%')"
        )
    sql = (
        "SELECT m.section_id, m.heading, m.text, m.page_from, m.page_to,"
        " m.file_id, f.period"
        " FROM mdna_section m JOIN file f ON f.file_id = m.file_id"
        f" WHERE f.project_id = ?{clause}"
        " ORDER BY f.period, m.page_from, m.section_id"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return con.execute(sql, params).fetchall()


def _section_pages(con: sqlite3.Connection, section: Any) -> dict[int, str]:
    """章节覆盖的那几页的正文。用于**精确定位主张在哪一页**。

    章节的 `page_from`/`page_to` 是一个范围，一个章节常跨好几页。
    只记 `page_from` 的话，后面几页的主张会被指到第一页去——
    点回原文点得到，但翻到的是别的内容。
    """
    lo, hi = section["page_from"], section["page_to"] or section["page_from"]
    if lo is None:
        return {}
    rows = con.execute(
        "SELECT page_no, text FROM document_page"
        " WHERE file_id = ? AND page_no BETWEEN ? AND ?",
        (section["file_id"], lo, hi),
    ).fetchall()
    return {r["page_no"]: (r["text"] or "") for r in rows}


def _locate_page(pages: dict[int, str], text: str, fallback: int | None) -> int | None:
    """在章节覆盖的页里找这句话出现在哪一页。

    去掉空白再比——年报原文里常有「财务费用  (五)56」这种多空格，
    模型复述时会把空格规范化掉。

    找不到就返回 None。**不返回 fallback**：调用方需要知道「没找到」，
    才能把它标成 needs_review。悄悄退回 `page_from` 的话，
    一个编造的句子会被当成有出处的。
    """
    if not pages:
        return fallback if fallback else None
    squeezed = "".join(text.split())
    if len(squeezed) < 6:      # 太短的话到处都是，匹配没有意义
        return None
    for page_no in sorted(pages):
        if squeezed in "".join(pages[page_no].split()):
            return page_no
    return None


def _build_row(
    *,
    claim_id: str,
    claim_type: str,
    section: Any,
    project_id: str,
    report_period: str,
    raw: dict,
    text: str,
    page_no: int,
    in_source: bool,
    prompt_version: str,
    extractor: str,
    now: str,
    metric_aliases: Sequence[str] = (),
) -> tuple[Any, ...]:
    """把一个模型返回的主张转成 `claim` 表的一行。

    `project_id` 与 `source_file_id` 都由调用方显式传入——
    它们都是 NOT NULL 且有外键，从 section 行里取的话一旦查询没选那一列，
    就会填成 NULL 然后在写库时被拒（而且是在整批写入的中途）。
    """
    direction = str(raw.get("direction") or "unknown")
    if direction not in ALLOWED_DIRECTIONS:
        direction = "unknown"

    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    # ★ 期间归一化**复用规则法基线**，不让模型自己给。
    #
    # 模型的强项是「读懂这句话在说什么」，弱项是「按我们的口径把它标准化」——
    # 期间口径一旦让它自由发挥，同一句话在两种抽取法下会指向不同年度，
    # 而那种不一致不会报错，只会让对照表看起来「两边结论不一样」。
    #
    # 2023 年报里说「2024 年公司计划……」→ 目标期间是 2024，用 2024 年的
    # 实际结果验证。搞反了会产生事后偏见的「处处支持」。
    period_expr = raw.get("period_expr") or extract_period_expr(text)
    period_norm = resolve_period(text, report_period) if report_period else None

    verifiable = bool(raw.get("verifiable")) and in_source and period_norm is not None
    if not in_source:
        confidence = 0.0    # 原句都找不到，置信度没有任何意义

    magnitude_value = raw.get("magnitude_value")
    magnitude_text = raw.get("magnitude_text")

    # ★ 幅度的**单位、界限、是不是计划值**同样复用规则法基线。
    #
    # 这三个信号原先一个都没落库：单位恒为 NULL、bound 算完就丢。
    # 后果不是「少了个字段」，而是判定层的两条分支**在活路径上永远为假**——
    # `is_explicit_target` 要求「比率型」看单位、「带界限的绝对量」看 bound，
    # 而 LLM 主张的单位永远是 None、bound 永远是默认的 exact。
    # 于是「增长 5% 以上」这类教科书式的明确数值目标，在模型法抽取下
    # 全部被当成没有目标的方向性主张走噪声带，**且不报错**。
    #
    # 让模型自己给也不行：它的强项是读懂句子，弱项是按我们的口径标准化。
    # 同一句话在两种抽取法下必须得到同一个 bound / is_plan，
    # 否则并排对照时两边结论不同，看不出是口径不同还是真的不同。
    probing = extract_magnitude(text, metric_aliases)
    magnitude_unit = probing.unit if probing and probing.unit else None
    magnitude_bound = probing.bound if probing else None
    is_plan_target = 1 if (probing and probing.is_plan) else 0
    # 同 bound / is_plan：**两种抽取法必须给出同一个值**，
    # 否则并排对照时两边的判定不同，看不出是口径不同还是真的不同。
    metric_aligned = 0 if (probing and not probing.metric_aligned) else 1

    return (
        claim_id,
        project_id,
        section["section_id"],
        text,
        raw.get("subject"),
        None,
        raw.get("object"),
        period_expr,
        period_norm,
        direction,
        magnitude_text,
        str(magnitude_value) if magnitude_value is not None else None,
        magnitude_unit,
        magnitude_bound,
        is_plan_target,
        metric_aligned,
        claim_type,
        1 if verifiable else 0,
        # v1.1 与 docs/01 的硬约束：不可验证的主张必须标 background_only
        0 if verifiable else 1,
        confidence,
        section["file_id"],
        page_no,
        text,          # source_text = 原句
        extractor,
        prompt_version,
        "validated" if verifiable else "needs_review",
        now,
    )


def _claim_id(section_id: str, text: str, claim_type: str, metric: str) -> str:
    """确定性 id，前缀 `cl-llm-` 与规则法的 `cl-` 区分开。

    两者并存是刻意的：验收标准要求「能和规则法对照」，
    用同一个 id 的话后写的会覆盖先写的，就对照不起来了。

    ⚠ **id 里必须带上 claim_type 与主判据。**

    只按 (章节, 原文) 算的话，同一句话被拆成两条不同主题的主张时
    会算出同一个 id——后一条被 `INSERT OR IGNORE` **静默丢掉**。

    而 v1.1 明确允许这种拆分：「一句话涉及多个指标的，拆成多条」。
    实测第一版就撞上了：40 条里 14 条被自己的 id 去重掉了。
    """
    normalized = " ".join(text.split())
    digest = hashlib.sha1(
        f"{section_id}\x00{normalized}\x00{claim_type}\x00{metric}".encode("utf-8")
    ).hexdigest()
    return "cl-llm-" + digest[:12]


# ---------------------------------------------------------------- 补齐


def backfill_missing_indicators(con: sqlite3.Connection, project_id: str) -> int:
    """给「有主张、没主判据」的 LLM 主张补上 `claim_indicator`。

    ## 为什么需要它

    第一版写 `claim` 时漏了 `claim_indicator`——结果 770 条 LLM 主张
    **一条都没参与判定**：匹配时读的是 `claim_indicator`，读不到就跳过，
    于是它们全落进「无主判据」那一堆，而**不报错**。
    页面上看起来只是「模型抽了很多但不能判」，实际是接线断了。

    ## 为什么是反查而不是重抽

    模型当时选的 `metric_key` 没有存下来，重抽要再花一遍钱（实测约 3 元）。
    而主题 → 主判据的映射就在规则层的主题表里，**反查是免费的**，
    且得到的口径与规则法完全一致——反而更利于对照。

    新抽取的会直接用模型给的 `metric_key`，不走这条兜底。
    """
    rows = con.execute(
        """
        SELECT c.claim_id, c.claim_type FROM claim c
        WHERE c.project_id = ? AND c.extractor LIKE 'llm:%'
          AND NOT EXISTS (
              SELECT 1 FROM claim_indicator ci WHERE ci.claim_id = c.claim_id
          )
        """,
        (project_id,),
    ).fetchall()

    payload = [
        (f"ci-{r['claim_id']}", r["claim_id"], _THEME_PRIMARY[r["claim_type"]],
         "primary", 0.5, "backfill:theme_rule")
        for r in rows
        if _THEME_PRIMARY.get(r["claim_type"])
    ]
    if not payload:
        return 0
    with con:
        con.executemany(
            "INSERT OR IGNORE INTO claim_indicator (id, claim_id, metric_key,"
            " role, match_confidence, matched_by) VALUES (?,?,?,?,?,?)",
            payload,
        )
    return len(payload)
