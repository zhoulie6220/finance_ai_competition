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
    industry_scope,
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
    #: 这一轮不再抽到、已经从库里删掉的旧主张数（多为收紧规则后露出来的表格行）
    pruned: int = 0
    #: 想删但身上挂着人工确认、**没有删**的。必须有人处理，见 warnings。
    prune_blocked: list[str] = field(default_factory=list)
    by_theme: dict[str, int] = field(default_factory=dict)
    unverifiable: int = 0
    #: 其中因为讲的是**全行业**而不是本公司而不进判定的。
    #: 与「没有期间」分开报——两件事该做的事完全不同：
    #: 一个去补抽取规则，一个本来就不该核。
    out_of_scope: int = 0
    pending: int = 0
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"扫描 {self.sections_scanned} 段 / {self.sentences_scanned} 句",
            f"写入 {self.claims_inserted} 条主张",
        ]
        if self.claims_skipped_existing:
            parts.append(f"已存在 {self.claims_skipped_existing} 条")
        if self.pruned:
            parts.append(f"清掉过期 {self.pruned} 条")
        parts.append(f"不可验证 {self.unverifiable} 条")
        if self.out_of_scope:
            parts.append(f"（其中全行业口径 {self.out_of_scope} 条）")
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

            # ★ **全行业口径的数字不锚定到本公司的报告年。**
            #
            # 「全年我国粗钢产量10.1亿吨，同比下降1.7%」拿到期间之后会被拿去
            # 和宝钢自己的钢材销量比：两个数一个是全国的、一个是公司的，
            # 而方向常常一致（都跟着钢周期走），判成「支持」，页面上看不出
            # 任何异常。**比错对象而结果看着合理，比重错更危险。**
            #
            # ⚠ 判据用的是 `sentence.context`（整段）而不是这一句——中文省略
            #   主语，「钢材产量14.00亿吨」单独看一个整体口径词都没有。
            scope_note = industry_scope(sentence.context or sentence.text)

            period = (
                None
                if scope_note is not None
                else resolve_period(
                    sentence.text,
                    section["period"],
                    # 取自 rule_config，这里给默认值；调用方可以覆盖
                    forward_verifies_next_year=True,
                )
            )
            if is_pending(period, known_periods):
                summary.pending += 1

            verifiable = match.usable and period is not None
            if not verifiable:
                summary.unverifiable += 1
                if scope_note is not None:
                    summary.out_of_scope += 1

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
        con.executemany(_UPSERT_CLAIM, rows)
        con.executemany(
            "INSERT OR IGNORE INTO claim_indicator (id, claim_id, metric_key,"
            " role, match_confidence, matched_by) VALUES (?,?,?,?,?,?)",
            indicators,
        )
        after = con.execute(
            "SELECT COUNT(*) FROM claim WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        summary.pruned, summary.prune_blocked = _prune_stale(
            con, project_id, {row[0] for row in rows}
        )

    summary.claims_inserted = after - before
    summary.claims_skipped_existing = len(rows) - summary.claims_inserted
    if summary.prune_blocked:
        summary.warnings.append(
            f"{len(summary.prune_blocked)} 条主张这一轮没再抽到，但它们身上有"
            f"会计的人工确认，**没有删**（删了会连确认一起级联删掉）。"
            f"请先跑 scripts/export_input_templates.py --rekey 把确认改挂到新编号上。"
        )
    return summary


#: 落库语句。**必须是 UPSERT，不能是 INSERT OR IGNORE。**
#:
#: ⚠ 用 `OR IGNORE` 时，抽取规则改了、重跑，「已经存在」的那些主张
#: **一个字段都不会更新** —— 改了 `resolve_period` 或口径守卫再跑一遍，
#: 进度照走、条数照报，库里的 `verifiable` / `period_norm` 却全是从前的值。
#: 表现出来就是「我明明改了，页面上数字一点没动」，
#: 和「改了但没重启服务」是同一种看不见的失败。
#:
#: `claim_id` 是确定性的（项目|章节|句序|归一化原文），所以冲突时更新的
#: 必然是**同一句话**，改它的属性是安全的——而且只有这样，
#: 会计已经填好的 `p_confirmation` 才不会被牵连（它按 claim_id 外键级联）。
_UPSERT_CLAIM = """
INSERT INTO claim (claim_id, project_id, section_id, claim_text, subject,
    action, object, period_expr, period_norm, direction, magnitude_text,
    magnitude_value, magnitude_unit, magnitude_bound, is_plan_target,
    magnitude_metric_aligned, claim_type, verifiable, background_only,
    confidence, source_file_id, source_page, source_text, extractor,
    prompt_version, status, created_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(claim_id) DO UPDATE SET
    period_expr = excluded.period_expr,
    period_norm = excluded.period_norm,
    direction = excluded.direction,
    magnitude_text = excluded.magnitude_text,
    magnitude_value = excluded.magnitude_value,
    magnitude_unit = excluded.magnitude_unit,
    magnitude_bound = excluded.magnitude_bound,
    is_plan_target = excluded.is_plan_target,
    magnitude_metric_aligned = excluded.magnitude_metric_aligned,
    claim_type = excluded.claim_type,
    verifiable = excluded.verifiable,
    background_only = excluded.background_only,
    confidence = excluded.confidence,
    source_file_id = excluded.source_file_id,
    source_page = excluded.source_page,
    source_text = excluded.source_text,
    status = excluded.status
"""


def _prune_stale(
    con: sqlite3.Connection, project_id: str, fresh_ids: set[str]
) -> tuple[int, list[str]]:
    """删掉这一轮**不再抽到**的旧主张。返回 (删了几条, 因故没删的 id)。

    ⚠ 为什么要删：只加不删的话，规则一收紧，老的坏主张会永远留在表里——
    分母虚高、页面上一堆已经不该存在的行，**而且不报错**。

    ⚠ 但删之前必须确认它身上**没有会计填的确认**：`p_confirmation.claim_id`
    是**级联删除**的，删一条主张会连带删掉那一行的复核记录，而那是人的活儿
    （2026-10-06 撞过一次同类事故：重建库把 121 行确认全作废）。

    做法：先按**归一化原文**把确认改挂到新编号上——编号漂了但原文没变，
    这正是 `scripts/export_input_templates.py --rekey` 的思路，这里用同一套
    判据（**只在唯一定位时才挂**）。挂不上就**不删**，交给调用方写进 warnings
    让人来处理。**不猜**。
    """
    stale = [
        r[0]
        for r in con.execute(
            "SELECT claim_id FROM claim WHERE project_id = ? AND extractor = ?",
            (project_id, EXTRACTOR),
        )
        if r[0] not in fresh_ids
    ]
    if not stale:
        return 0, []

    by_text: dict[str, list[str]] = {}
    for cid, text in con.execute(
        "SELECT claim_id, claim_text FROM claim WHERE project_id = ? AND extractor = ?",
        (project_id, EXTRACTOR),
    ):
        if cid in fresh_ids:
            by_text.setdefault(" ".join((text or "").split()), []).append(cid)

    blocked: list[str] = []
    doomed: list[str] = []
    for cid in stale:
        row = con.execute(
            "SELECT p.claim_id FROM p_confirmation p WHERE p.claim_id = ?", (cid,)
        ).fetchone()
        if row is None:
            doomed.append(cid)
            continue
        text = con.execute(
            "SELECT claim_text FROM claim WHERE claim_id = ?", (cid,)
        ).fetchone()
        key = " ".join((text[0] if text else "").split())
        hit = by_text.get(key, [])
        # 目标编号上**已经有确认**时也不能挂 —— `p_confirmation` 上
        # 有 UNIQUE(claim_id)，硬挂会撞约束、整批回滚。
        if len(hit) == 1 and con.execute(
            "SELECT 1 FROM p_confirmation WHERE claim_id = ?", (hit[0],)
        ).fetchone() is None:
            con.execute(
                "UPDATE p_confirmation SET claim_id = ? WHERE claim_id = ?",
                (hit[0], cid),
            )
            doomed.append(cid)
        else:
            blocked.append(cid)

    con.executemany("DELETE FROM claim WHERE claim_id = ?", [(c,) for c in doomed])
    return len(doomed), blocked


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
