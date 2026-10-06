"""规则法主张抽取的编排与落库。

**这一层持有数据库连接**，`app/engine/claim_rules.py` 与
`app/parsing/claims.py` 都是纯文本处理、零 IO。

## 为什么强调「规则法基线」

甲的阶段二要做 LLM 版主张抽取，原话是「在规则法基线上叠，不是从零写」。
基线的作用有两个：

1. **兜底**：模型不可用（没配密钥、断网、超时）时系统仍然能出主张，
   而不是整条链路瘫掉。现场演示时这一点很实际。
2. **对照**：LLM 版的结果要和规则版并排看，分歧点进人工复核。
   没有基线就没法判断模型到底带来了什么。

## 确定性 claim_id

`cl-` + sha1(project_id|section_id|句子序号|归一化文本)[:12]。

重跑一次会往表里再插一份，n 和 N 同时翻倍、指数整体偏移——**而且不报错**。
确定性主键让重跑插到同一行上（配 INSERT OR IGNORE），这是防重复的**唯一**
可靠手段。

为什么不加唯一索引：v1.1 §A.2 明确允许「同一句涉及不同指标可拆分」，
所以 (project_id, section_id, claim_text) 不是自然键，加了会误杀合法主张。
而把可空列拼进去也不行——SQLite 里 NULL 互不相等，只要有一列是 NULL，
索引就形同虚设，那比没有更糟。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.db.dictionary import metric_aliases

from app.engine.claim_rules import (
    Magnitude,
    extract_direction,
    extract_magnitude,
    extract_period_expr,
    is_pending,
    match_theme,
    resolve_period,
)
from app.parsing.claims import iter_sentences

EXTRACTOR = "rule:claim_v1"
# 规则法没有提示词，但这一列 NOT NULL——如实写「无」，
# 不要填一个看起来像 prompt 版本号的假值。
PROMPT_VERSION = "rule:none"

# 规则的置信度：按「命中主题词数 + 是否有方向 + 是否有数值」给一个档。
# **不用模型给的分**——规则法的确定性恰恰是它的价值，编一个模型式的
# 小数反而让人以为它经过校准。
_CONFIDENCE_BASE = 0.5
_CONFIDENCE_WITH_DIRECTION = 0.7
_CONFIDENCE_WITH_MAGNITUDE = 0.8


@dataclass
class ExtractionSummary:
    """一次抽取的结果。**如实报告跳过了什么**，不只报成功的数。"""

    project_id: str
    sections_scanned: int = 0
    sentences_scanned: int = 0
    claims_inserted: int = 0
    claims_skipped_existing: int = 0
    by_theme: dict[str, int] = field(default_factory=dict)
    unverifiable: int = 0
    pending: int = 0
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"扫描 {self.sections_scanned} 段 / {self.sentences_scanned} 句",
            f"写入 {self.claims_inserted} 条主张",
        ]
        if self.claims_skipped_existing:
            parts.append(f"已存在 {self.claims_skipped_existing} 条")
        parts.append(f"不可验证 {self.unverifiable} 条")
        if self.pending:
            parts.append(f"未到期 {self.pending} 条")
        return "；".join(parts)


def extract_and_store(
    con: sqlite3.Connection,
    project_id: str,
    *,
    now: str,
) -> ExtractionSummary:
    """抽取主张并落库。幂等：重复跑不会产生重复主张。"""
    summary = ExtractionSummary(project_id=project_id)

    known_periods = tuple(
        r[0]
        for r in con.execute(
            "SELECT DISTINCT period FROM file WHERE project_id = ? AND period IS NOT NULL",
            (project_id,),
        )
    )
    # 主判据指标的别名，用来在一句多目标时挑对那个数（见 claim_rules._pick_by_metric）。
    # 取一次、整批复用——字段字典在抽取期间不会变。
    aliases_by_metric = metric_aliases(con)

    sections = con.execute(
        """
        SELECT m.section_id, m.text, m.page_from, m.file_id, f.period
        FROM mdna_section m
        JOIN file f ON f.file_id = m.file_id
        WHERE f.project_id = ?
        ORDER BY f.period, m.page_from, m.section_id
        """,
        (project_id,),
    ).fetchall()

    rows: list[tuple[Any, ...]] = []
    indicators: list[tuple[Any, ...]] = []

    for section in sections:
        summary.sections_scanned += 1
        for sentence in iter_sentences(section["text"] or ""):
            summary.sentences_scanned += 1
            match = match_theme(sentence.text)
            if match is None:
                continue

            direction, modality_only = extract_direction(sentence.text)
            # 别名用来在一句多目标时挑对那个数。宝钢的年度经营计划是
            # 「计划产铁X万吨、…、营业成本Z亿元」，不传别名会取到产铁的吨数。
            magnitude = extract_magnitude(
                sentence.text,
                aliases_by_metric.get(match.rule.primary_metric or "", ()),
            )

            # 只提了主题词、没有方向也没有数值 → 不是主张，是背景叙述。
            # 放进 claim 表会让 N 虚高、覆盖率虚高，而页面上一堆「主张」
            # 根本判定不了。
            if direction == "unknown" and magnitude is None:
                continue

            period = resolve_period(
                sentence.text,
                section["period"],
                # 取自 rule_config，这里给默认值；调用方可以覆盖
                forward_verifies_next_year=True,
            )
            if is_pending(period, known_periods):
                summary.pending += 1

            verifiable = match.usable and period is not None
            if not verifiable:
                summary.unverifiable += 1

            claim_id = _claim_id(
                project_id, section["section_id"], sentence.index, sentence.text
            )
            rows.append(
                (
                    claim_id,
                    project_id,
                    section["section_id"],
                    sentence.text,            # 原句，不改写
                    None,                     # subject：规则法不拆，留给 LLM 版
                    None,                     # action
                    None,                     # object
                    extract_period_expr(sentence.text),   # 原文里的期间表述，原样保留
                    period,
                    direction,
                    magnitude.raw if magnitude else None,
                    str(magnitude.value) if magnitude else None,
                    magnitude.unit if magnitude else None,
                    magnitude.bound if magnitude else None,
                    1 if (magnitude and magnitude.is_plan) else 0,
                    # 这个数是不是靠主判据别名取到的。**判定层拿不到原文与
                    # 别名表**，所以只能在这里定下来（同 magnitude_bound）。
                    # 默认 1：没有幅度时谈不上「错位」。
                    0 if (magnitude and not magnitude.metric_aligned) else 1,
                    match.rule.claim_type,
                    1 if verifiable else 0,
                    # v1.1 与 docs/01 的硬约束：不可验证的主张必须标
                    # background_only，**不得进入评分**
                    0 if verifiable else 1,
                    _confidence(direction, magnitude, modality_only),
                    section["file_id"],
                    section["page_from"],
                    sentence.text,            # source_text = 原句
                    EXTRACTOR,
                    PROMPT_VERSION,
                    "validated" if verifiable else "needs_review",
                    now,
                )
            )
            if match.usable and match.rule.primary_metric:
                indicators.append(
                    (
                        f"ci-{claim_id}",
                        claim_id,
                        match.rule.primary_metric,
                        "primary",
                        _confidence(direction, magnitude, modality_only),
                        EXTRACTOR,
                    )
                )
            summary.by_theme[match.rule.label_cn] = (
                summary.by_theme.get(match.rule.label_cn, 0) + 1
            )

    if not rows:
        summary.warnings.append(
            "没有抽出任何主张。先确认 mdna_section 里有正文——"
            "空库或未导入数据包时会走到这里。"
        )
        return summary

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
        con.executemany(
            "INSERT OR IGNORE INTO claim_indicator (id, claim_id, metric_key,"
            " role, match_confidence, matched_by) VALUES (?,?,?,?,?,?)",
            indicators,
        )
        after = con.execute(
            "SELECT COUNT(*) FROM claim WHERE project_id = ?", (project_id,)
        ).fetchone()[0]

    summary.claims_inserted = after - before
    summary.claims_skipped_existing = len(rows) - summary.claims_inserted
    return summary


def _claim_id(
    project_id: str, section_id: str, sentence_index: int, text: str
) -> str:
    """确定性 id。见模块开头的说明——防重跑重复靠的是它，不是唯一索引。"""
    normalized = " ".join(text.split())
    digest = hashlib.sha1(
        f"{project_id}\x00{section_id}\x00{sentence_index}\x00{normalized}".encode(
            "utf-8"
        )
    ).hexdigest()
    return "cl-" + digest[:12]


def _confidence(
    direction: str, magnitude: Magnitude | None, modality_only: bool
) -> float:
    """规则法的置信度。**是规则档位，不是校准过的概率。**

    意向表述（「力争」「计划」）降一档——v1.1 明确它们不是保证承诺，
    拿它们当实打实的承诺去验证会夸大冲突。
    """
    if magnitude is not None:
        score = _CONFIDENCE_WITH_MAGNITUDE
    elif direction != "unknown":
        score = _CONFIDENCE_WITH_DIRECTION
    else:
        score = _CONFIDENCE_BASE
    if modality_only:
        score -= 0.2
    return round(max(0.0, min(1.0, score)), 2)


# ---------------------------------------------------------------- 查询


def list_claims(
    con: sqlite3.Connection,
    project_id: str,
    *,
    theme: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """主张列表，带主题规则里那份「禁止的简化推断」，供页面展示。"""
    params: list[Any] = [project_id]
    clause = ""
    if theme:
        clause = " AND c.claim_type = ?"
        params.append(theme)
    params.append(limit)

    rows = con.execute(
        f"""
        SELECT c.claim_id, c.claim_text, c.claim_type, c.direction, c.period_norm,
               c.period_expr, c.magnitude_text, c.magnitude_value, c.magnitude_unit,
               c.verifiable, c.background_only, c.confidence, c.status,
               c.source_page, c.source_file_id, c.extractor,
               ci.metric_key AS primary_metric
        FROM claim c
        LEFT JOIN claim_indicator ci
          ON ci.claim_id = c.claim_id AND ci.role = 'primary'
        WHERE c.project_id = ?{clause}
        ORDER BY c.period_norm, c.claim_type, c.claim_id
        LIMIT ?
        """,
        params,
    ).fetchall()

    from app.engine.claim_rules import THEMES

    forbidden_by_type = {t.claim_type: t.forbidden for t in THEMES}
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        item["forbidden_simplifications"] = list(
            forbidden_by_type.get(r["claim_type"], ())
        )
        out.append(item)
    return out


def claim_stats(con: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """抽取概况。页面首屏用。"""
    row = con.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN verifiable = 1 THEN 1 ELSE 0 END) AS verifiable,
               SUM(CASE WHEN background_only = 1 THEN 1 ELSE 0 END) AS background_only,
               SUM(CASE WHEN status = 'validated' THEN 1 ELSE 0 END) AS validated
        FROM claim WHERE project_id = ?
        """,
        (project_id,),
    ).fetchone()
    by_type = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT claim_type, COUNT(*) FROM claim WHERE project_id = ?"
            " GROUP BY claim_type",
            (project_id,),
        )
    }
    return {
        "total": row["total"] or 0,
        "verifiable": row["verifiable"] or 0,
        "background_only": row["background_only"] or 0,
        "validated": row["validated"] or 0,
        "by_type": by_type,
    }
