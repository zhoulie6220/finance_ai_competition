"""主张 × 事实的匹配编排与落库。

与 `app/skills/narrative.py`（抽取）分开，因为这是两个关注点：
抽取决定「有哪些主张」，匹配决定「这些主张站不站得住」。

引擎（`app/engine/claim_match.py`）是纯函数，这一层持有数据库连接。

## H 与 C 怎么分

用**报告的年份**和**主张指向的年份**比：

    target > 报告年  → 前瞻主张 → **H**（历史兑现度）
    target ≤ 报告年  → 当期主张 → **C**（当期一致性）

2023 年报里说「2024 年公司计划……」：目标期间 2024 大于报告年 2023，
所以它是前瞻主张，用 2024 年的实际结果验证——这正是 H 的定义
（「以前报告的前瞻主张，其目标期间已结束、验证数据已公开」）。

v1.1 要求**同一主张不得同时计入 H 和 C**，这个分类天然互斥。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.engine.claim_match import (
    ActualValue,
    ClaimInput,
    MatchConfig,
    judge,
)
from app.skills import claim_scope

#: 判定值 → s 标度。**只有这三个计分**，其余三个不计分也不进分子。
VERDICT_TO_S: dict[str, Decimal] = {
    "supported": Decimal(1),
    "neutral": Decimal(0),
    "contradicted": Decimal(-1),
}


@dataclass
class MatchSummary:
    project_id: str
    total: int = 0
    matched: int = 0
    #: 没有主判据、因而未落 claim_match 的主张数（产品结构升级、产能释放那两类）
    no_metric: int = 0
    #: 标成「背景」、因而不进判定表的主张数。**它和 no_metric 是两回事**：
    #: 前者压根没有判据，后者有判据但没有可核对的期间/对象。
    background: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    history_scores: list[Decimal] = field(default_factory=list)
    current_scores: list[Decimal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        c = self.counts
        return (
            f"共 {self.total} 条主张，判定 {self.matched} 条"
            + (f"（另有 {self.no_metric} 条无主判据、不进分母）" if self.no_metric else "")
            # ⚠ 背景主张这一条**必须报出来**：不报的话，「判定数从 344 掉到 186」
            #   看起来像判定变少了，而实际是那批本来就不该进表。
            + (f"（{self.background} 条标为背景，见「未纳入判定的主张」一节）"
               if self.background else "")
            + "——"
            f"支持 {c.get('supported', 0)}、无明显变化 {c.get('neutral', 0)}、"
            f"相悖 {c.get('contradicted', 0)}、待核查 {c.get('needs_review', 0)}、"
            f"不可验证 {c.get('unverifiable', 0)}、不可比 {c.get('incomparable', 0)}"
        )

    @property
    def scored(self) -> int:
        return len(self.history_scores) + len(self.current_scores)


# ---------------------------------------------------------------- 取数


def facts_index(
    con: sqlite3.Connection, project_id: str
) -> dict[tuple[str, str], ActualValue]:
    """把事实读成 (指标, 期间) → 实际值 的索引。

    只读**已验证**的行——指数与估值的查询只允许读可信数据，
    这是本项目在 SQL 层就设好的边界。
    """
    rows = con.execute(
        """
        SELECT metric_key, period, value_millions, fact_id, comparable,
               incomparable_reason
        FROM financial_fact
        WHERE project_id = ? AND scope = 'consolidated' AND status = 'validated'
        ORDER BY period, metric_key, fact_id
        """,
        (project_id,),
    ).fetchall()

    out: dict[tuple[str, str], ActualValue] = {}
    for r in rows:
        raw = r["value_millions"]
        if raw is None:
            continue
        try:
            value = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            continue
        # 同一 (指标, 期间) 可能有多个 period_kind 的行。setdefault 配合
        # ORDER BY 保证取到的是确定的那一条——不确定的话，同一份数据
        # 两次跑会得到不同的判定。
        out.setdefault(
            (r["metric_key"], r["period"]),
            ActualValue(
                metric_key=r["metric_key"],
                period=r["period"],
                value=value,
                fact_id=r["fact_id"],
                comparable=bool(r["comparable"]),
                incomparable_reason=r["incomparable_reason"],
            ),
        )
    return out


def config_from_rules(con: sqlite3.Connection) -> MatchConfig:
    """噪声阈值从 rule_config 读。

    **不用代码默认值**——页面上「查看 / 修改 / 恢复默认」改的就是那张表。
    两者分叉之后，页面显示的口径和实际生效的会对不上，而且不报错。
    """
    rows = {
        r["key"]: r["value"]
        for r in con.execute(
            "SELECT key, value FROM rule_config WHERE key LIKE 'narrative.%'"
        )
    }
    mapping = {
        "narrative.min_rel_change": "min_rel_change",
        "narrative.min_ratio_change": "min_ratio_change",
        "narrative.min_days_change": "min_days_change",
        "narrative.min_utilization_change": "min_utilization_change",
    }
    kwargs: dict[str, Decimal] = {}
    for key, attr in mapping.items():
        if key in rows:
            kwargs[attr] = Decimal(rows[key])
    return MatchConfig(**kwargs) if kwargs else MatchConfig()


def prior_period(period: str) -> str | None:
    """上一个年度。非四位年度（如 2024H1）不支持——本批数据只有年度。"""
    if len(period) != 4 or not period.isdigit():
        return None
    return str(int(period) - 1)


# ---------------------------------------------------------------- 匹配


def _claim_input(
    r: sqlite3.Row, *, metric: str | None = None, substituted: bool = False
) -> ClaimInput:
    """把一行查询结果装配成判定输入。

    **两个查询（落库 / 装配指数）共用它。** 各写一套的后果不是报错：
    同一批主张在判定表和指数里会用上不同的量纲或方向，
    **而两边都算得出数**。绝对量目标这一批正好依赖这两个字段，
    分开写就一定会分叉。
    """
    magnitude = None
    if r["magnitude_value"] is not None:
        try:
            magnitude = Decimal(str(r["magnitude_value"]))
        except Exception:  # noqa: BLE001
            magnitude = None
    return ClaimInput(
        claim_id=r["claim_id"],
        claim_text=r["claim_text"] if "claim_text" in r.keys() else "",
        claim_type=r["claim_type"],
        direction=r["direction"] or "unknown",
        period_norm=r["period_norm"],
        magnitude_value=magnitude,
        magnitude_unit=r["magnitude_unit"],
        magnitude_raw=r["magnitude_text"],
        bound=r["magnitude_bound"] or "exact",
        # `metric` 传了就用它——那是 `effective_metric` 算出来的**实际判据**，
        # 可能与 `claim_indicator` 里存的原判据不同（会计授权的回退）。
        primary_metric=r["primary_metric"] if metric is None else metric,
        metric_substituted=substituted,
        is_plan=bool(r["is_plan_target"]),
        # 量纲与 sign_convention 来自字段字典（会计口径 §A-1 / §二）。
        # 绝对量目标要靠它们判「能不能比」和「往哪个方向算达成」——
        # 拿不到就转人工复核，不拿 S 代默认值硬判。
        metric_unit_kind=r["unit_kind"],
        metric_sign=r["sign_convention"],
        target_metric_aligned=bool(r["magnitude_metric_aligned"]),
    )


#: 会计授权的**判据回退**：主判据在这个期间没有数据时，允许改用哪个指标。
#:
#: ⚠ **这不是「缺数据就换个指标硬判」**——那正是 v1.1 点名禁止的
#: （「主判据未披露时必须标记不可验证，不能降级用代理指标直接作出
#: 冲突结论」）。这里是**会计逐条授权**的一个例外，有案可查：
#: 2026-10-06 第六轮答复「改走 B，但加严格限制」——华菱「钢铁行业」、
#: 首钢「冶金」两个聚合行的销量，年报没说明是钢材还是粗钢，
#: 允许**只用于「需求与产销」的方向判断与 Q3**，前提是保留年报原样
#: 行名/单位/出处、只比较相邻年度同一行名同一单位同一披露范围的数据。
#:
#: 所以这张表**只有一项**，别的新增都要会计先点头。
#: 别的主题、别的指标都没有这个口子。
_FALLBACK_METRIC: dict[str, str] = {
    "steel_sales_volume": "industry_sales_volume",
}


def effective_metric(
    metric: str | None,
    period: str | None,
    facts: dict[tuple[str, str], ActualValue],
) -> tuple[str | None, bool]:
    """返回 (实际用来判定的指标, 是否发生了回退)。

    ⚠ 只在**主判据这个期间确实没数据、而回退指标有**时才换。
    两者都缺时保持原样——那样判定会如实地说「未披露 steel_sales_volume」，
    而不是悄悄拿另一个指标顶上。

    ⚠ 回退指标与原判据的**量纲必须一致**（这里两个都是吨、
    都是 positive_is_good）。不一致的话 `_claim_input` 带下去的
    `metric_unit_kind` 会是原判据的，绝对量目标那道量纲闸门就判错了。
    加新回退项时**先看这一步**。
    """
    if not metric or not period or (metric, period) in facts:
        return metric, False
    alt = _FALLBACK_METRIC.get(metric)
    if alt and (alt, period) in facts:
        return alt, True
    return metric, False


#: 判定要用到的 claim 列。两个查询共用，避免一处加了字段另一处没加。
_CLAIM_SELECT = """
    SELECT c.claim_id, c.claim_text, c.claim_type, c.direction, c.period_norm,
           c.magnitude_value, c.magnitude_unit, c.magnitude_text,
           c.magnitude_bound, c.is_plan_target, c.magnitude_metric_aligned,
           ci.metric_key AS primary_metric,
           md.unit_kind, md.sign_convention, md.label_cn AS metric_label,
           c.claim_text, c.source_page, c.background_only,
           f.period AS report_period
    FROM claim c
    JOIN file f ON f.file_id = c.source_file_id
    LEFT JOIN claim_indicator ci
      ON ci.claim_id = c.claim_id AND ci.role = 'primary'
    LEFT JOIN metric_definition md ON md.metric_key = ci.metric_key
"""


def match_and_store(
    con: sqlite3.Connection, project_id: str, *, now: str
) -> MatchSummary:
    """跑匹配并落 `claim_match`。**整批替换**，不做增量追加。

    增量追加会在重跑之后留下上一轮的旧结论，页面上新旧混排且不报错。
    删除与写入在同一事务里，中途失败整体回滚。
    """
    cfg = config_from_rules(con)
    facts = facts_index(con, project_id)

    scope, scope_params = claim_scope.scope_sql("c")
    rows = con.execute(
        f"{_CLAIM_SELECT} WHERE c.project_id = ?{scope} ORDER BY c.claim_id",
        (project_id, *scope_params),
    ).fetchall()

    summary = MatchSummary(project_id=project_id)
    payload: list[tuple[Any, ...]] = []

    summary.no_metric = 0
    for r in rows:
        summary.total += 1
        period = r["period_norm"]
        metric = r["primary_metric"]

        # 没有主判据的主张（产品结构升级、产能释放那两类）**不落 claim_match**：
        # 该表的 metric_key 有外键指向 metric_definition，空串会违反外键。
        # 它们在语义上也不该出现——v1.1 规定这类不进覆盖率分母。
        # 页面靠 claim.background_only 识别它们，不需要 match 行。
        if not metric:
            summary.no_metric += 1
            continue

        # ⚠ **标成「背景」的主张也不落。**
        #
        # 上面那句「页面靠 `claim.background_only` 识别它们」写的是**意图**，
        # 但这里原来只挡了 `not metric` 一条。实测宝钢有 **158 条**
        # `background_only=1` 的主张带着主判据（有指标、没期间），
        # 于是照样被落了行、判成 `unverifiable`，混进「主张—事实对照表」。
        #
        # 后果在页面上一眼可见：判定表 344 条里 161 条是「不可验证」，
        # 看着像这个系统的数据缺口巨大；而那 158 条**根本不是可核验的主张**，
        # 页面另有一节「未纳入判定的主张」专门放它们（`BackgroundClaims`）。
        # **两节同时显示同一批句子**，还给了两种不同的说法。
        if r["background_only"]:
            summary.background += 1
            continue

        # 会计授权的判据回退（只有华菱/首钢的行业聚合销量一处，见
        # `effective_metric` 的 docstring）。回退过的话**必须传下去**——
        # 判定理由里要写明「本条不是用钢材销量判的」，否则两种理由长得一样。
        metric, substituted = effective_metric(metric, period, facts)

        current = facts.get((metric, period)) if (metric and period) else None
        prior = prior_period(period) if period else None
        base = facts.get((metric, prior)) if (metric and prior) else None

        outcome = judge(
            _claim_input(r, metric=metric, substituted=substituted),
            current=current, base=base, cfg=cfg,
        )
        summary.matched += 1
        summary.counts[outcome.verdict] = summary.counts.get(outcome.verdict, 0) + 1

        s = VERDICT_TO_S.get(outcome.verdict)
        if s is not None:
            report_period = r["report_period"]
            if period and report_period and period > report_period:
                summary.history_scores.append(s)
            else:
                summary.current_scores.append(s)

        payload.append(
            (
                f"m-{r['claim_id']}",
                r["claim_id"],
                metric or "",
                current.fact_id if current else None,
                period or "",
                outcome.fact_period or period or "",
                outcome.direction_claim,
                outcome.direction_actual,
                None
                if outcome.direction_consistent is None
                else (1 if outcome.direction_consistent else 0),
                outcome.magnitude_target,
                outcome.magnitude_actual,
                str(outcome.relative_deviation)
                if outcome.relative_deviation is not None
                else None,
                outcome.verdict,
                outcome.reason,
                outcome.confidence,
                outcome.formula,
                json.dumps({"fact_ids": list(outcome.inputs)}, ensure_ascii=False)
                if outcome.inputs
                else None,
                outcome.target_unit,
                str(outcome.target_millions)
                if outcome.target_millions is not None
                else None,
                str(outcome.unit_factor) if outcome.unit_factor is not None else None,
                str(outcome.plan_variance) if outcome.plan_variance is not None else None,
                outcome.plan_reference,
                now,
            )
        )

    if not payload:
        summary.warnings.append(
            "没有可匹配的主张。先跑一次主张抽取（skills/narrative.py）。"
        )
        return summary

    with con:
        con.execute(
            "DELETE FROM claim_match WHERE claim_id IN"
            " (SELECT claim_id FROM claim WHERE project_id = ?)",
            (project_id,),
        )
        con.executemany(
            "INSERT OR REPLACE INTO claim_match (match_id, claim_id, metric_key,"
            " fact_id, claim_period, fact_period, direction_claim, direction_actual,"
            " direction_consistent, magnitude_target, magnitude_actual,"
            " relative_deviation, verdict, reason, confidence, formula, inputs,"
            " target_unit, target_millions, unit_factor, plan_variance,"
            " plan_reference, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
    return summary


def load_matches(
    con: sqlite3.Connection, project_id: str, *, limit: int = 500
) -> list[dict[str, Any]]:
    """读回判定结果，供页面展示。"""
    rows = con.execute(
        """
        SELECT m.match_id, m.claim_id, m.metric_key, m.verdict, m.reason,
               m.confidence, m.claim_period, m.fact_period, m.direction_claim,
               m.direction_actual, m.magnitude_target, m.magnitude_actual,
               m.relative_deviation, m.formula, m.inputs,
               -- 绝对量目标的换算留痕与「原始计划偏差」。页面要能显示
               -- 「2,420 亿元 × 100 = 242,000 百万元」，否则用户只看到一个
               -- 换算过的数，看不出它从哪来（会计口径 8-1 的用意就在这里）。
               m.target_unit, m.target_millions, m.unit_factor,
               m.plan_variance, m.plan_reference,
               c.claim_text, c.claim_type, c.source_page, c.verifiable,
               c.background_only
        FROM claim_match m
        JOIN claim c ON c.claim_id = m.claim_id
        WHERE c.project_id = ?
        ORDER BY m.verdict, m.claim_period DESC, m.match_id
        LIMIT ?
        """,
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ================================================================ 指数装配


def verdict_counts(con: sqlite3.Connection, project_id: str) -> dict[str, int]:
    """按判定分组计数。**分母口径与引擎保持一致**，不在这里重新解释。"""
    rows = con.execute(
        """
        SELECT m.verdict, COUNT(*) AS n
        FROM claim_match m JOIN claim c ON c.claim_id = m.claim_id
        WHERE c.project_id = ? GROUP BY m.verdict
        """,
        (project_id,),
    ).fetchall()
    return {r["verdict"]: r["n"] for r in rows}


def project_index_input(
    con: sqlite3.Connection, project_id: str
) -> tuple[Any, dict[str, Any]]:
    """装配指数的输入。

    返回 (IndexInput, 诊断信息)。

    ## H 与 C 的取值

    报表期的年份与主张指向的年份比：target > 报告年 → 前瞻主张 → H；
    否则 → C。判定值映射到 s 标度（supported +1 / neutral 0 / contradicted −1）。

    ## N 与 n

    N = 判定过、且**目标可识别**的主张数（unverifiable 与 incomparable 不进分母）；
    n = 其中拿到 supported / neutral / contradicted 的条数。

    ⚠ 覆盖率的分母**不含 unverifiable**，这不是放水：v1.1 §A.4 对 N 的定义是
    「已到验证期、对象和目标可识别」的主张数。「未披露直接指标」的条目不满足
    「目标可识别」，把它们算进分母会让覆盖率永远上不去，而那是数据缺口
    而不是判定质量问题——两者不该混在一个指标里。
    """
    scored, counters = _scored_claims(con, project_id)
    history = [Decimal(str(d["s"])) for d in scored if d["bucket"] == "h"]
    current = [Decimal(str(d["s"])) for d in scored if d["bucket"] == "c"]
    n = counters["n"]
    N = counters["N"]
    no_period = counters["no_period"]
    no_fact = counters["no_fact"]

    from app.engine.index import IndexInput
    quality, q2_pairs = _quality_component(con, project_id)
    q2_done = sum(1 for p in q2_pairs if p["done"])

    return (
        IndexInput(
            history_scores=tuple(history),
            current_scores=tuple(current),
            observation_count=n,
            denominator_count=N,
            risk=_risk_component(con, project_id),
            template=_template_component(con, project_id),
            quality=quality,
        ),
        {
            "n": n, "N": N,
            "history_count": len(history),
            "current_count": len(current),
            "skipped_no_period": no_period,
            "skipped_no_fact": no_fact,
            "q2_pairs": q2_pairs,
            "q2_done": q2_done,
            "q2_total": len(q2_pairs),
        },
    )


def _scored_claims(
    con: sqlite3.Connection, project_id: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """逐条判定，返回**计分了的那批主张**与覆盖率计数。

    ⚠ 这个函数是 H/C 的**唯一**实现。指数的分项值和页面的下钻明细
    都从这里取——各写一套的话，下钻里显示 12 条而指数用的是 13 条，
    **两边都算得出来，而且看不出哪个对**。这正是本项目反复要挡的那类错。

    每条记录带上「点回原文」需要的全部字段：期间、指标、主张原文、
    出处页码、对比用的事实值、判定理由、算式与代入的数。
    """
    facts = facts_index(con, project_id)

    scope, scope_params = claim_scope.scope_sql("c")
    rows = con.execute(
        f"{_CLAIM_SELECT} WHERE c.project_id = ?{scope} ORDER BY c.claim_id",
        (project_id, *scope_params),
    ).fetchall()

    cfg = config_from_rules(con)
    out: list[dict[str, Any]] = []
    n = 0
    N = 0
    no_period = 0
    no_fact = 0

    for r in rows:
        metric = r["primary_metric"]
        period = r["period_norm"]
        if not metric:
            continue
        if not period:
            # 句子本身没有期间表述 → 目标不可识别 → 不进分母
            no_period += 1
            continue

        # 与 `match_and_store` **同一处回退**。两处不一致的话，
        # 判定表用的是行业聚合销量、指数用的是钢材销量——
        # 而两边都算得出数，看不出它们不是一套。
        metric, substituted = effective_metric(metric, period, facts)

        cur = facts.get((metric, period))
        prior = prior_period(period)
        base = facts.get((metric, prior)) if prior else None

        outcome = judge(
            _claim_input(r, metric=metric, substituted=substituted),
            current=cur, base=base, cfg=cfg,
        )
        if cur is None:
            no_fact += 1

        if outcome.in_denominator:
            N += 1
        s = VERDICT_TO_S.get(outcome.verdict)
        if s is None:
            continue
        n += 1
        report_period = r["report_period"]
        bucket = "h" if (report_period and period > report_period) else "c"
        out.append({
            "bucket": bucket,
            "s": str(s),
            "claim_id": r["claim_id"],
            # 主张原文截断到 160 字：整句可能几百字，下钻列表里读不完，
            # 而完整原文在「叙事一致性」的判定表里能点开。
            "claim_text": (r["claim_text"] or "")[:160],
            "claim_source_page": r["source_page"],
            "report_period": report_period,
            "period": period,
            "metric_key": metric,
            "metric_label": r["metric_label"] or metric,
            "verdict": outcome.verdict,
            "reason": outcome.reason,
            "direction_claim": outcome.direction_claim,
            "direction_actual": outcome.direction_actual,
            "magnitude_target": outcome.magnitude_target,
            "magnitude_actual": outcome.magnitude_actual,
            "relative_deviation": outcome.relative_deviation,
            "formula": outcome.formula,
            # `inputs` 是「代入公式的数」，落库时存成 JSON 数组。
            # 它不是 dict——写成 `dict(...)` 会在这里抛 ValueError，
            # 而整个指数接口跟着 500。
            "inputs": list(outcome.inputs or ()),
            "fact_id": cur.fact_id if cur else None,
        })

    return out, {
        "n": n, "N": N,
        "no_period": no_period,
        "no_fact": no_fact,
    }


#: 五个分项的中文名与权重。**与 `engine/index.py::IndexConfig` 同源**——
#: 改权重的地方只有那一处，这里只是显示用的副本。
COMPONENT_META: dict[str, tuple[str, str]] = {
    "h": ("历史兑现度 H", "上一年度 MD&A 前瞻性表述与下一年度实际结果的匹配程度"),
    "c": ("当期一致性 C", "本期主张与同期财务事实的一致程度"),
    "r": ("风险披露充分度 R", "四项固定风险是否同时说清对象、路径与可核验依据"),
    "p": ("模板化惩罚 P", "进入审查的实质经营表述里，缺乏可验证性的占比"),
    "q": ("财务质量冲突 Q", "三项固定质量检查里已确认触发的占比"),
}


def component_detail(
    con: sqlite3.Connection, project_id: str, key: str, *, limit: int = 400
) -> dict[str, Any]:
    """指数某个分项的**下钻明细**：这个分数由哪些记录构成。

    ⚠ **H/C 的明细与指数用的是同一次计算**（都走 `_scored_claims`）。
    各写一套的话，下钻里显示 12 条而指数用的是 13 条，
    **两边都算得出来，而且看不出哪个对**——这正是本项目反复要挡的那类错。

    每一行都带「点回原文」要的东西：期间、指标、主张原文、出处页码、
    对比用的事实、判定理由、算式与代入的数。评委问「这 6.48 分是哪些
    记录构成的」，这一层就是答案。
    """
    if key not in COMPONENT_META:
        raise KeyError(key)
    label_cn, _ = COMPONENT_META[key]
    rows: list[dict[str, Any]] = []
    note: str | None = None

    if key in ("h", "c"):
        scored, _ = _scored_claims(con, project_id)
        picked = [d for d in scored if d["bucket"] == key]
        for d in picked:
            rows.append({
                "kind": "claim",
                "label": d["claim_text"] or "(主张原文缺失)",
                "detail": d["reason"] or "",
                "contribution": d["s"],
                "period": d["period"],
                "report_period": d["report_period"],
                "metric_label": d["metric_label"],
                "source_page": d["claim_source_page"],
                "source_text": None,
                "reviewer": None,
                "claim_id": d["claim_id"],
                "fact_id": d["fact_id"],
                "formula": d["formula"],
                "inputs": d["inputs"],
                "verdict": d["verdict"],
            })
        note = (
            "s 取值 +1（支持）/ 0（无明显变化）/ −1（相悖），分项值是它们的**等权平均**。"
            "相悖的条目是本分项的主要拖累，点开可看算式与代入的数。"
        )

    elif key == "r":
        for r in con.execute(
            "SELECT item, applicable, risk_object, impact_path, evidence, mitigation,"
            " conclusion, source_page, reviewer"
            " FROM risk_disclosure_check WHERE project_id = ?"
            " ORDER BY period, item", (project_id,),
        ):
            rows.append({
                "kind": "risk",
                "label": _RISK_ITEM_CN.get(r["item"], r["item"]),
                "detail": "；".join(x for x in (
                    f"风险对象：{r['risk_object']}" if r["risk_object"] else "",
                    f"作用路径：{r['impact_path']}" if r["impact_path"] else "",
                    f"依据：{r['evidence']}" if r["evidence"] else "",
                ) if x) or "（未填）",
                "contribution": "充分" if r["conclusion"] == "sufficient" else r["conclusion"],
                "period": None,
                "report_period": None,
                "metric_label": None,
                "source_page": r["source_page"],
                "source_text": r["evidence"],
                "reviewer": r["reviewer"],
                "claim_id": None,
                "fact_id": None,
                "formula": None,
                "inputs": [],
                "verdict": r["conclusion"],
            })
        note = (
            "R = 判为 sufficient 的项数 ÷ 适用项数。判据是**同时**说清风险对象、"
            "作用路径与可核验依据；只有「公司面临原材料价格波动风险」这种不算。"
        )

    elif key == "p":
        for r in con.execute(
            "SELECT p.claim_id, p.is_substantive, p.is_template, p.missing_elements,"
            " p.conclusion, p.reviewer, c.claim_text, c.source_page"
            " FROM p_confirmation p JOIN claim c ON c.claim_id = p.claim_id"
            " WHERE c.project_id = ? ORDER BY p.conclusion, p.claim_id",
            (project_id,),
        ):
            rows.append({
                "kind": "template",
                "label": (r["claim_text"] or "")[:160],
                "detail": "缺 " + "、".join(
                    _ELEMENT_CN.get(x, x)
                    for x in (json.loads(r["missing_elements"] or "[]"))
                ) if r["missing_elements"] not in (None, "", "[]") else "四要素齐备",
                "contribution": "计罚" if r["conclusion"] == "p_penalty" else r["conclusion"],
                "period": None,
                "report_period": None,
                "metric_label": None,
                "source_page": r["source_page"],
                "source_text": None,
                "reviewer": r["reviewer"],
                "claim_id": r["claim_id"],
                "fact_id": None,
                "formula": None,
                "inputs": [],
                "verdict": r["conclusion"],
            })
        note = (
            "P = 判为 p_penalty 的条数 ÷ 进入审查的实质经营表述条数。"
            "**这一列只能人工定**：模型只出候选（落在 `p_prediction`），"
            "碰不到这里。分母是「实质经营表述」，非实质的 0 不进分母。"
        )

    else:  # q
        # ⚠ **必须走 `_quality_inputs`，不能就地重建清单。**
        #   我第一版就是就地重建的，结果漏传了 `inventory_days_gap` 与
        #   `sales_volume_change`，下钻里 Q3 显示「未核验」而指数里它是已核验的
        #   ——两个页面都渲染得出来，只是说的是两件事。
        checklist, q2_outcomes, _ = _quality_inputs(con, project_id)
        aging = _load_q2_aging(con, project_id)
        for item in checklist.applicable:
            rows.append({
                "kind": "quality",
                "label": item.label_cn,
                "detail": item.detail or item.note or "",
                "contribution": (
                    "触发" if item.triggered else "未触发"
                ) if item.verified else "未核验",
                "period": None, "report_period": None, "metric_label": None,
                "source_page": None, "source_text": item.note or None,
                "reviewer": None, "claim_id": None, "fact_id": None,
                "formula": item.detail or None, "inputs": [],
                "verdict": "triggered" if item.triggered else "not_triggered",
            })
        for i, o in enumerate(q2_outcomes, start=1):
            prior_row, cur_row = aging[i - 1], aging[i]
            rows.append({
                "kind": "quality",
                "label": (f"Q2 应收周转天数与账龄同时恶化"
                          f"（{prior_row.period}→{cur_row.period}）"),
                "detail": o.reason or "",
                "contribution": (
                    ("触发" if o.triggered else "未触发") if o.ok else "待核查"
                ),
                "period": cur_row.period, "report_period": None, "metric_label": None,
                "source_page": cur_row.source_page,
                # 「点回原文」在这一项上指向会计抄录的原文片段，
                # 而不是 PDF 页——这一格的数据源是人工录入，不是解析。
                "source_text": cur_row.source_text,
                "reviewer": cur_row.reviewer,
                "claim_id": None, "fact_id": None,
                "formula": o.formula or None, "inputs": [],
                "verdict": o.status,
            })
        note = (
            "Q = 已确认触发的项数 ÷ 已核验的适用项数。Q2 逐对比较 9 个年度，"
            "用的是会计逐条抄录的账龄数据。"
        )

    truncated = len(rows) > limit
    if truncated:
        # ⚠ 截断必须说出来。不说的话，页面看起来像「一共就这么多条」，
        # 而分母又是全量——两个数凑不上，读的人只会以为自己看错了。
        note = (note or "") + f"（只显示前 {limit} 条，共 {len(rows)} 条）"
    return {
        "key": key,
        "label_cn": label_cn,
        "rows": rows[:limit],
        "total": len(rows),
        "truncated": truncated,
        "note": note,
    }


#: `risk_disclosure_check.item` 的取值 → 中文。与 `engine/attestation.RISK_ITEMS` 同源。
_RISK_ITEM_CN = {
    "demand_price": "需求与钢价",
    "fuel_cost": "原燃料成本",
    "environment_capacity": "环保及产能",
    "liquidity_collection": "流动性与回款",
}

#: `missing_elements` 的四要素 → 中文。口径 §4.2。
_ELEMENT_CN = {
    "object": "对象", "period": "期间", "metric": "指标",
    "result": "结果", "owner": "责任人",
}


def _load_q2_aging(con: sqlite3.Connection, project_id: str) -> list[Any]:
    """读会计按 §2.3 抄进来的账龄数据。"""
    from decimal import Decimal, InvalidOperation

    from app.engine.q2 import AgingYear

    rows = con.execute(
        "SELECT period, receivable_gross, over_one_year, revenue, status, note,"
        " source_page, source_text, reviewer"
        " FROM q2_aging WHERE project_id = ? ORDER BY period",
        (project_id,),
    ).fetchall()

    def dec(raw: Any) -> Decimal | None:
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            return None

    return [
        AgingYear(
            period=r["period"],
            receivable_gross=dec(r["receivable_gross"]),
            over_one_year=dec(r["over_one_year"]),
            revenue=dec(r["revenue"]),
            status=r["status"],
            note=r["note"],
            source_page=r["source_page"],
            source_text=r["source_text"],
            reviewer=r["reviewer"],
        )
        for r in rows
    ]


def _load_risk_items(con: sqlite3.Connection, project_id: str) -> list[Any]:
    """读 R 的检查记录，**按「项目年度 × 四个固定项」逐格展开**。

    ⚠ **没录入的格子要补成 pending，不能只返回已录入的那几条。**
    只返回已录入的话，四项里填了一项、那一项恰好是 sufficient，
    R 就会算出 1/1 = 100% —— 而实际是有三项根本没核。
    比例看起来完美，恰恰因为大部分没做。

    ## ⚠ 这里原来是**随机的**（2026-10-06 修）

    原来的写法是按 `item` 建字典：

        rows = { r["item"]: r for r in con.execute("SELECT ... WHERE project_id=?") }

    它**完全不管年度**，而且查询**没有 `ORDER BY`**——同一个 `item` 有 10 行
    （10 个年度），字典同键后写覆盖前写，**哪一年胜出取决于 SQLite 的返回顺序**。

    后果很具体：华菱的 R 算不出来，**仅仅因为 2024 年那三行 `needs_review`
    恰好胜出**；换台机器、换一次查询计划，同一批数据可能算出另一个数。
    而「R 不可算」和「R = 0」在页面上长得一模一样。

    现在按**项目的每个年度 × 四个固定项**展开，缺哪一格补 `pending`——
    与导出给会计的 `R_风险检查.csv`（`年度 × 项`，一行一格）**逐格对齐**。

    > ⚠ **粒度（公司级 4 项 vs 年度×项）是个口径问题，已打包问会计**
    > （`给会计的材料-20261006-R表口径.zip`，见 `待会计确认.md` 问题 16）。
    > 这里先按 CSV 的结构取，理由是 CSV 才是会计实际填的那个东西；
    > 而且这个读法**更严**（要求全部格子核完），不会把 R 抬高。
    """
    from app.engine.attestation import RISK_ITEMS, RiskItemResult

    years = [
        r["value"] for r in con.execute(
            "SELECT value FROM json_each("
            "  (SELECT fiscal_years FROM project WHERE project_id = ?))"
            " ORDER BY value",
            (project_id,),
        )
    ]
    rows = {
        (r["period"], r["item"]): r
        for r in con.execute(
            "SELECT period, item, applicable, conclusion, risk_object,"
            " impact_path, evidence"
            " FROM risk_disclosure_check WHERE project_id = ?"
            " ORDER BY period, item",
            (project_id,),
        )
    }
    out = []
    for year in years:
        for key, _label in RISK_ITEMS:
            r = rows.get((year, key))
            if r is None:
                out.append(RiskItemResult(
                    item=key, applicable=True, conclusion="pending", period=year,
                ))
            else:
                out.append(RiskItemResult(
                    item=key,
                    applicable=bool(r["applicable"]),
                    conclusion=r["conclusion"],
                    risk_object=r["risk_object"],
                    impact_path=r["impact_path"],
                    evidence=r["evidence"],
                    period=year,
                ))
    return out


def _load_p_confirmations(con: sqlite3.Connection, project_id: str) -> list[Any]:
    """读 P 的人工确认记录。

    ⚠ 分母是「**进入审查的实质经营表述**」，而那批表述由人判定。
    所以这里只看 `p_confirmation` 表里已有的记录——
    人工还没开始审的主张**不在这张表里**，也就不会被误算成
    「已确认无问题」。这一点和 R 不同：R 的项是固定的四项，
    没录就是没核；P 的候选是流动的，没录就是还没审到。

    会计口径 §4.4 要求「所有进入指数的实质经营表述必须完成逐条确认」，
    所以只要还有记录是 `pending`，P 就不完整。

    ⚠ **候选集要走 `claim_scope`。** 这是「进入指数的实质经营表述」，
    而标成背景的主张**不进指数**——它们已经不是主张了。
    不过滤的后果实测发生过：华菱有 10 行确认记录挂在一批**已经降级为背景**
    的主张上、状态停在 `pending`，于是 **P 永远不完整、华菱永远出不了分**，
    而页面上只会说「10 条实质表述尚未人工确认」——听起来像「还没审到」，
    实际是「这几句已经不是主张了」。**卡在什么东西上，从提示里看不出来。**
    """
    import json

    from app.engine.attestation import PConfirmation

    rows = con.execute(
        f"SELECT p.claim_id, p.is_substantive, p.conclusion, p.is_template,"
        f" p.missing_elements, p.reviewer"
        f" FROM p_confirmation p JOIN claim c ON c.claim_id = p.claim_id"
        f" WHERE c.project_id = ?{claim_scope.scope_sql('c')[0]}",
        (project_id, *claim_scope.scope_sql("c")[1]),
    ).fetchall()

    out = []
    for r in rows:
        try:
            missing = tuple(json.loads(r["missing_elements"] or "[]"))
        except (ValueError, TypeError):
            missing = ()
        out.append(
            PConfirmation(
                claim_id=r["claim_id"],
                is_substantive=bool(r["is_substantive"]),
                conclusion=r["conclusion"],
                is_template=None if r["is_template"] is None else bool(r["is_template"]),
                missing_elements=missing,
                reviewer=r["reviewer"],
            )
        )
    return out


def _risk_component(con: sqlite3.Connection, project_id: str) -> Any:
    """R：四项风险披露检查（会计口径 §3.2）。

    数据从 `risk_disclosure_check` 读——**那是人工填的**，
    会计口径点名禁止用风险词频代替：「披露充分不代表风险小」。
    没录入的项按 `pending` 计，让 R 变成「未核验」而不是「全通过」。
    """
    from app.engine.attestation import risk_component

    return risk_component(_load_risk_items(con, project_id))


def _template_component(con: sqlite3.Connection, project_id: str) -> Any:
    """P：缺乏可验证性的实质表述占比（会计口径 §4.2）。

    ⚠ P **只能人工定**：「模型只能提出候选，不能自动定P」。
    表是空的时候返回「未核验」，不返回 0。
    """
    from app.engine.attestation import template_component

    return template_component(_load_p_confirmations(con, project_id))


def _quality_component(con: sqlite3.Connection, project_id: str) -> tuple[Any, list[dict]]:
    """Q：三项固定检查的汇总（会计口径 §4.3）。

    Q1、Q3 由程序从财务事实算；**Q2 需要人工抄进来的账龄数据**。
    Q2 只要有一个年度判不了，Q 就是「不完整」——按 §五
    「Q状态 incomplete，不假设未触发」。

    三项合起来算一个比例：已确认触发的项数 / 已核验的适用项数。

    返回 `(Q 分项, Q2 逐对比较的明细)`。明细是给**页面**用的：
    2026-10-06 会计答复原话「页面可以另外展示『8/9组比较已完成，
    1组因原始披露缺失待核查』」——只给一句「Q 不可算」的话，
    读的人分不出「差一对比」和「差得远」，而这两件事要做的事完全不同。
    """
    checklist, q2_outcomes, q2_comp = _quality_inputs(con, project_id)
    base = checklist.to_component()

    # ---- 三项合一 -----------------------------------------------------
    items = list(checklist.applicable)
    verified = [i for i in items if i.verified]
    triggered = [i for i in verified if i.triggered]

    # Q2 也算一项：判得了就进分母，判不了就让整体不完整
    if q2_comp.verified:
        verified_count = len(verified) + 1
        triggered_count = len(triggered) + q2_comp.numerator
        q2_ok = True
    else:
        verified_count = len(verified)
        triggered_count = len(triggered)
        q2_ok = False

    notes = [n for n in (base.note, q2_comp.note) if n]
    if q2_outcomes and not q2_ok:
        notes.append(
            f"Q2 的 {len(q2_outcomes)} 个比较年度里有判不了的——"
            f"按 §五 Q 不完整，不假设未触发"
        )
    elif not q2_outcomes:
        notes.append(
            "Q2 尚无账龄数据（`q2_aging` 表为空）——"
            "请会计按 `scripts/export_input_templates.py` 导出的模板逐年抄录"
        )

    from app.engine.index import RatioComponent

    aging = _load_q2_aging(con, project_id)
    pairs = [
        {
            "prior": aging[i - 1].period,
            "current": aging[i].period,
            "status": o.status,
            # 判得了 = 拿到了 triggered / not_triggered
            "done": o.triggered is not None,
            "reason": o.reason,
        }
        for i, o in enumerate(q2_outcomes, start=1)
    ]

    return RatioComponent(
        name="quality_conflict",
        label_cn="财务质量冲突 Q",
        numerator=triggered_count,
        denominator=verified_count,
        verified=base.verified and q2_ok and verified_count > 0,
        note="；".join(notes),
    ), pairs


def _quality_inputs(
    con: sqlite3.Connection, project_id: str
) -> tuple[Any, list[Any], Any]:
    """Q 的三项检查清单 + Q2 的逐对判定。**`_quality_component` 与底下的下钻共用它。**

    ⚠ 各写一套的后果不是报错。实测撞到过：下钻里 Q3 显示「未核验」
    而指数里它是已核验的——因为下钻那次重建清单时**少传了
    `inventory_days_gap` 与 `sales_volume_change`**。
    两个页面都渲染得出来，只是说的是两件事。抽在这里让它不可能分叉。
    """
    from app.engine.quality import build_checklist, turnover_days
    from app.engine.q2 import judge_q2, to_component as q2_component

    cfo = _series(con, project_id, "cfo")
    ni = _series(con, project_id, "net_income")
    inventory = _series(con, project_id, "inventory")
    cost = _series(con, project_id, "operating_cost")
    years = tuple(sorted(set(cfo) & set(ni)))

    inventory_days_gap = None
    sales_volume_change = None
    common = sorted(set(inventory) & set(cost))
    if len(common) >= 3:
        a, b, cur = common[-3], common[-2], common[-1]
        now_days = turnover_days(inventory[cur], inventory[b], cost[cur])
        before = turnover_days(inventory[b], inventory[a], cost[b])
        if now_days is not None and before is not None:
            inventory_days_gap = now_days - before

        # ---- Q3 的另一个输入：同口径钢材销量同比 ------------------------
        #
        # ⚠ **年度必须与上面那一对数严格对齐**（都是 b → cur）。
        #   各自取「最近两个可用年度」的话，销量可能是 2019→2020 而存货是
        #   2023→2024 —— 两个数都算得出来，凑在一起判出来的是**两回事**。
        #
        # ⚠ 库里只有宝钢的钢材销量。会计 2026-10-06 答复明确：
        #   华菱「钢铁行业」、首钢「冶金」两个聚合行**不自动映射**到本字段，
        #   所以另两家这里拿到的是 None，Q3 照旧判不了 ——
        #   而不是拿空序列当成「销量没降」。
        sales = _series(con, project_id, "steel_sales_volume")
        if b in sales and cur in sales and sales[b]:
            sales_volume_change = (sales[cur] - sales[b]) / abs(sales[b])

    # ---- Q2：从人工抄录的账龄数据判定 --------------------------------
    aging = _load_q2_aging(con, project_id)
    q2_outcomes = [
        judge_q2(aging[i - 1], aging[i]) for i in range(1, len(aging))
    ]
    q2_comp = q2_component(q2_outcomes)

    # ---- Q1 / Q3：从财务事实算 ---------------------------------------
    checklist = build_checklist(
        cfo_by_year=cfo,
        net_income_by_year=ni,
        years=years,
        receivable_days_gap=None,      # 归 Q2 管，不重复计
        aging_share_gap=None,
        inventory_days_gap=inventory_days_gap,
        sales_volume_change=sales_volume_change,
        # ⚠ 这一句不能省。只喂 None 而不声明「Q2 在别处判」的话，
        #   这一项会以 applicable=True / verified=False 的状态留在清单里，
        #   把 `all_verified` 永久钉死成 False —— Q 就永远算不出来。
        #   详见 `build_checklist` 的 docstring。
        receivable_aging_handled_elsewhere=True,
    )
    return checklist, q2_outcomes, q2_comp


def _series(
    con: sqlite3.Connection, project_id: str, metric: str
) -> dict[str, Decimal]:
    """某指标按年度的值。取第一条，顺序确定——不确定会让两次跑结果不同。"""
    rows = con.execute(
        "SELECT period, value_millions FROM financial_fact"
        " WHERE project_id = ? AND metric_key = ? AND status = 'validated'"
        " AND scope = 'consolidated' AND length(period) = 4"
        " ORDER BY period, fact_id",
        (project_id, metric),
    ).fetchall()
    out: dict[str, Decimal] = {}
    for r in rows:
        if r["value_millions"] is None:
            continue
        try:
            out.setdefault(r["period"], Decimal(str(r["value_millions"])))
        except Exception:  # noqa: BLE001
            continue
    return out
