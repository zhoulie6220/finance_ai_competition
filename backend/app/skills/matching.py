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
    counts: dict[str, int] = field(default_factory=dict)
    history_scores: list[Decimal] = field(default_factory=list)
    current_scores: list[Decimal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        c = self.counts
        return (
            f"共 {self.total} 条主张，判定 {self.matched} 条"
            + (f"（另有 {self.no_metric} 条无主判据、不进分母）" if self.no_metric else "")
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


def _claim_input(r: sqlite3.Row) -> ClaimInput:
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
        primary_metric=r["primary_metric"],
        is_plan=bool(r["is_plan_target"]),
        # 量纲与 sign_convention 来自字段字典（会计口径 §A-1 / §二）。
        # 绝对量目标要靠它们判「能不能比」和「往哪个方向算达成」——
        # 拿不到就转人工复核，不拿 S 代默认值硬判。
        metric_unit_kind=r["unit_kind"],
        metric_sign=r["sign_convention"],
        target_metric_aligned=bool(r["magnitude_metric_aligned"]),
    )


#: 判定要用到的 claim 列。两个查询共用，避免一处加了字段另一处没加。
_CLAIM_SELECT = """
    SELECT c.claim_id, c.claim_text, c.claim_type, c.direction, c.period_norm,
           c.magnitude_value, c.magnitude_unit, c.magnitude_text,
           c.magnitude_bound, c.is_plan_target, c.magnitude_metric_aligned,
           ci.metric_key AS primary_metric,
           md.unit_kind, md.sign_convention,
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

        current = facts.get((metric, period)) if (metric and period) else None
        prior = prior_period(period) if period else None
        base = facts.get((metric, prior)) if (metric and prior) else None

        outcome = judge(_claim_input(r), current=current, base=base, cfg=cfg)
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
    facts = facts_index(con, project_id)

    scope, scope_params = claim_scope.scope_sql("c")
    rows = con.execute(
        f"{_CLAIM_SELECT} WHERE c.project_id = ?{scope} ORDER BY c.claim_id",
        (project_id, *scope_params),
    ).fetchall()

    cfg = config_from_rules(con)
    history: list[Decimal] = []
    current: list[Decimal] = []
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

        cur = facts.get((metric, period))
        prior = prior_period(period)
        base = facts.get((metric, prior)) if prior else None

        outcome = judge(_claim_input(r), current=cur, base=base, cfg=cfg)
        if cur is None:
            no_fact += 1

        if outcome.in_denominator:
            N += 1
        s = VERDICT_TO_S.get(outcome.verdict)
        if s is None:
            continue
        n += 1
        report_period = r["report_period"]
        if report_period and period > report_period:
            history.append(s)
        else:
            current.append(s)

    from app.engine.index import IndexInput

    return (
        IndexInput(
            history_scores=tuple(history),
            current_scores=tuple(current),
            observation_count=n,
            denominator_count=N,
            risk=_risk_component(con, project_id),
            template=_template_component(con, project_id),
            quality=_quality_component(con, project_id),
        ),
        {
            "n": n,
            "N": N,
            "history_count": len(history),
            "current_count": len(current),
            # 如实报告跳过了什么，不只报成功的数
            "skipped_no_period": no_period,
            "skipped_no_fact": no_fact,
        },
    )


def _load_q2_aging(con: sqlite3.Connection, project_id: str) -> list[Any]:
    """读会计按 §2.3 抄进来的账龄数据。"""
    from decimal import Decimal, InvalidOperation

    from app.engine.q2 import AgingYear

    rows = con.execute(
        "SELECT period, receivable_gross, over_one_year, revenue, status"
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
        )
        for r in rows
    ]


def _load_risk_items(con: sqlite3.Connection, project_id: str) -> list[Any]:
    """读 R 的四项检查记录。

    ⚠ **没录入的项要补成 pending，不能只返回已录入的那几条。**
    只返回已录入的话，四项里填了一项、那一项恰好是 sufficient，
    R 就会算出 1/1 = 100% —— 而实际是有三项根本没核。
    比例看起来完美，恰恰因为大部分没做。
    """
    from app.engine.attestation import RISK_ITEMS, RiskItemResult

    rows = {
        r["item"]: r
        for r in con.execute(
            "SELECT item, applicable, conclusion, risk_object, impact_path,"
            " evidence FROM risk_disclosure_check WHERE project_id = ?",
            (project_id,),
        )
    }
    out = []
    for key, _label in RISK_ITEMS:
        r = rows.get(key)
        if r is None:
            out.append(
                RiskItemResult(item=key, applicable=True, conclusion="pending")
            )
        else:
            out.append(
                RiskItemResult(
                    item=key,
                    applicable=bool(r["applicable"]),
                    conclusion=r["conclusion"],
                    risk_object=r["risk_object"],
                    impact_path=r["impact_path"],
                    evidence=r["evidence"],
                )
            )
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
    """
    import json

    from app.engine.attestation import PConfirmation

    rows = con.execute(
        "SELECT p.claim_id, p.is_substantive, p.conclusion, p.is_template,"
        " p.missing_elements, p.reviewer"
        " FROM p_confirmation p JOIN claim c ON c.claim_id = p.claim_id"
        " WHERE c.project_id = ?",
        (project_id,),
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


def _quality_component(con: sqlite3.Connection, project_id: str) -> Any:
    """Q：三项固定检查的汇总（会计口径 §4.3）。

    Q1、Q3 由程序从财务事实算；**Q2 需要人工抄进来的账龄数据**。
    Q2 只要有一个年度判不了，Q 就是「不完整」——按 §五
    「Q状态 incomplete，不假设未触发」。

    三项合起来算一个比例：已确认触发的项数 / 已核验的适用项数。
    """
    from app.engine.quality import build_checklist
    from app.engine.q2 import judge_q2, to_component as q2_component

    cfo = _series(con, project_id, "cfo")
    ni = _series(con, project_id, "net_income")
    inventory = _series(con, project_id, "inventory")
    cost = _series(con, project_id, "operating_cost")
    years = tuple(sorted(set(cfo) & set(ni)))

    from app.engine.quality import turnover_days

    inventory_days_gap = None
    common = sorted(set(inventory) & set(cost))
    if len(common) >= 3:
        a, b, cur = common[-3], common[-2], common[-1]
        now_days = turnover_days(inventory[cur], inventory[b], cost[cur])
        before = turnover_days(inventory[b], inventory[a], cost[b])
        if now_days is not None and before is not None:
            inventory_days_gap = now_days - before

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
        sales_volume_change=None,      # 钢材销量在库中 0 行
    )
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

    return RatioComponent(
        name="quality_conflict",
        label_cn="财务质量冲突 Q",
        numerator=triggered_count,
        denominator=verified_count,
        verified=base.verified and q2_ok and verified_count > 0,
        note="；".join(notes),
    )


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
