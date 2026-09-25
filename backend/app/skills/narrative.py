"""Skill · 叙事一致性：管理层说的话，财务事实认不认。

这是这个项目和「AI 财报摘要工具」的分界线。摘要工具把 MD&A 读一遍、总结成一段话，
读起来很像那么回事，但**没有一句能核对**——它说的「公司强调降本增效」，
你没法问它兑现了没有。这里的每一步都能：

    原句（哪一年年报第几页）→ 命中哪类主题 → 拿哪个指标验 → 那个指标从多少走到多少

## 数据从哪来

`mdna_section`（`scripts/parse_mdna.py` 落的正文）+ `v_fact_verified`（财务事实）。
两边都不在本文件里解析、计算：句子切分在 `app.parsing.mdna`，
方向判定在 `app.engine.narrative`。**本文件只做取数与组织。**

## 为什么不给分数

`rule_config` 里有完整的指数公式与五项构成（H 历史兑现度 / C 当前一致性 /
R 风险披露变化 / P 模板化惩罚 / Q 财务质量冲突）。这里只算得出 H 和 C：
R 与 P 要风险段落与跨年文本相似度，Q 要 `engine/checks.py` 的勾稽结果，
那个还没写。

**只算得出两项的时候就出分，是最糟的选择**：分母没变、权重照乘，
分数看起来和完整版一模一样，而它其实缺了足足 30 分权重的构成项。
所以这里出**观测与计数**，把分数留给 `engine/index.py`——
等五项齐全、且 `docs/04` 经会计签字后再开。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from app.engine.narrative import (
    DEFAULT_MIN_REL_CHANGE,
    INCOMPARABLE,
    MISSING,
    STATE_CN,
    THEMES,
    Claim,
    Observation,
    Utterance,
    find_claims,
    summarize,
    verify_all,
)
from app.engine.ratios import gross_margin
from app.parsing.mdna import Section, paragraphs
from app.skills.base import PlannedStep, SkillRequest, StepOutcome, tool_result
from app.skills.facts import _read_series, _resolve_project
from app.tools.registry import ToolOutcome, tool

if TYPE_CHECKING:
    from app.agents.orchestrator import StepContext

#: 叙事验证要用到的指标。**在这里写死是有意的**：
#: 主题表（`engine.narrative._THEMES`）里每个主题指向哪个指标是会计口径的一部分，
#: 从数据库里现查一遍反而会让人以为它是可配置的。
_METRICS = ("revenue", "operating_cost", "cfo")


def _rule(con, key: str, fallback: str) -> str:
    """读一个规则参数。读不到就用默认值——**并让调用方知道是默认值**。

    `rule_config` 是 docs/04 的机读版本，改数据不改代码。这里读不到通常意味着
    库是旧的（没跑 `init_db.py`），那时用默认值继续比直接报错更有用，
    但绝不能装作它是确认过的取值——所以取默认值时调用方会在结论里写出来。
    """
    row = con.execute(
        "SELECT value FROM rule_config WHERE key=? AND industry=''", (key,)
    ).fetchone()
    return row["value"] if row else fallback


def _load_utterances(con, project_id: str) -> list[Utterance]:
    """把 MD&A 正文取出来，切成带页码的句子。

    ⚠ `mdna_section` 是**按页存**的（一段跨几页就几行，`page_from = page_to`），
    所以页码在这里是精确的。若当初按整段存，这一步就只能给全段盖一个起始页，
    证据面板上每一句的页码都会指向同一页——而用户真的会翻过去看。
    """
    rows = con.execute(
        "SELECT s.heading, s.kind, s.page_from, s.text, f.period, f.rel_path"
        " FROM mdna_section s JOIN file f ON f.file_id = s.file_id"
        " WHERE f.project_id=?"
        " ORDER BY f.period, s.page_from",
        (project_id,),
    ).fetchall()

    out: list[Utterance] = []
    for r in rows:
        sec = Section(
            heading=r["heading"], kind=r["kind"],
            page_from=r["page_from"], page_to=r["page_from"],
            lines=tuple((r["page_from"], ln) for ln in r["text"].split("\n")),
        )
        # 只留文件名，不带目录。`samples/主公司年报-宝钢股份600019/宝钢股份：2019年年度报告.pdf`
        # 里的目录名是本地磁盘的布局，对用户没有意义，还占掉证据面板一整行。
        fname = r["rel_path"].rsplit("/", 1)[-1]
        for p in paragraphs(sec):
            out.append(
                Utterance(
                    text=p.display,
                    page_no=p.page_no,
                    period=r["period"],
                    forward=r["kind"] == "outlook",
                    source_file=fname,
                )
            )
    return out


def _metric_series(con, project_id: str) -> dict[str, dict[str, Decimal | None]]:
    """取出验证要用的指标序列。**毛利率是派生的**，年报里没有这一行。"""
    raw: dict[str, dict[str, Decimal]] = {}
    for key in _METRICS:
        points, _, _ = _read_series(con, project_id, key)
        raw[key] = {p["period"]: Decimal(p["value"]) for p in points}

    rev, cost = raw["revenue"], raw["operating_cost"]
    gm: dict[str, Decimal | None] = {}
    for year in set(rev) | set(cost):
        if year not in rev or year not in cost:
            # ⚠ 缺一个就置 None，**不要跳过这个键**。
            #   跳过的后果是 verify() 用 `.get()` 拿到 None，判成「缺失」——
            #   结论一样，但「这一年没披露」和「我没查它」在调查报告里分不出来。
            gm[year] = None
            continue
        r = gross_margin(rev[year] - cost[year], rev[year])
        gm[year] = r.value if r.ok else None

    return {"revenue": rev, "operating_cost": cost, "cfo": raw["cfo"], "gross_margin": gm}


def _claim_payload(c: Claim) -> dict[str, Any]:
    """一条主张的形状。前端 `types/view.ts` 里有对应定义，改这里要同步。"""
    return {
        "theme_key": c.theme_key,
        "theme_label": c.theme_label,
        "text": c.text,
        "page_no": c.page_no,
        "source_file": c.source_file,
        "matched": c.matched,
        "source_period": c.source_period,
        "verify_period": c.verify_period,
        "forward": c.forward,
        "metric_key": c.metric.key,
        "metric_label": c.metric.label_cn,
        "metric_unit": c.metric.unit,
        "basis_cn": THEMES[c.theme_key].basis_cn,
    }


def _obs_payload(ob: Observation, src: dict[str, Any] | None) -> dict[str, Any]:
    d = _claim_payload(ob.claim)
    d.update(
        {
            "state": ob.state,
            "state_cn": ob.state_cn,
            "reason": ob.reason,
            "formula": ob.formula,
            "inputs": ob.inputs,
            "actual": ob.actual,
            # 财务那一侧的出处。只有句子出处、没有数字出处的对照表，
            # 用户能核对「管理层真这么说了」，却核对不了「数字真的是这样」——
            # 而这张表的全部意义就是把两者摆在一起。
            "metric_source": src or {},
        }
    )
    return d


def _source_of(con, project_id: str, metric_key: str, period: str) -> dict[str, Any]:
    """取某个指标某一年的事实出处。"""
    key = metric_key
    if key == "gross_margin":
        # 派生值没有单一出处，它的出处是「收入 − 成本」两行，
        # 与 `facts.margin` 的处理一致——只给一个的话界面上是半个算式。
        rev = _source_of(con, project_id, "revenue", period)
        cost = _source_of(con, project_id, "operating_cost", period)
        if not rev and not cost:
            return {}
        return {
            "derived": True,
            "sources": [
                {"role": "被减数 · 营业收入", **rev},
                {"role": "减数 · 营业成本", **cost},
            ],
        }
    points, label, unit = _read_series(con, project_id, key)
    for p in points:
        if p["period"] == period:
            return {
                "label_cn": label, "unit": unit, "period": period,
                "value": p["value"], "fact_id": p["fact_id"],
                "source_file": p["source_file"], "source_page": p["source_page"],
                "source_table": p["source_table"], "source_text": p["source_text"],
            }
    return {}


# --------------------------------------------------------------- Tool 实现


@tool(
    name="narrative.claims",
    version="v1",
    description_cn="从年报的管理层讨论与分析里抽取可验证的经营主张，逐条带出年报页码与原句",
    input_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string", "description": "项目 id"}},
    },
    output_schema={
        "type": "object",
        "properties": {"claims": {"type": "array"}, "total": {"type": "integer"}},
    },
)
def narrative_claims(ctx: StepContext, project_id: str | None = None) -> ToolOutcome:
    con = ctx.repo.con
    pid, err = _resolve_project(con, project_id)
    if err:
        return ToolOutcome(summary=f"无法读取叙事：{err}", value={"error": err})

    utterances = _load_utterances(con, pid)
    if not utterances:
        # 「正文没入库」和「公司没说什么」在界面上长得很像，但要做的事完全不同：
        # 前者去跑 scripts/parse_mdna.py，后者才需要看年报。
        return ToolOutcome(
            summary="这个项目还没有 MD&A 正文。先跑：python scripts/parse_mdna.py",
            value={"project_id": pid, "claims": [], "total": 0, "no_text": True},
        )

    claims = find_claims(utterances)
    by_theme: dict[str, int] = {}
    for c in claims:
        by_theme[c.theme_label] = by_theme.get(c.theme_label, 0) + 1
    detail = "、".join(f"{k} {v}" for k, v in by_theme.items()) or "无"

    return ToolOutcome(
        summary=(
            f"从 {len(utterances)} 句管理层表述中识别出 {len(claims)} 条可验证主张"
            f"（{detail}）"
        ),
        value={
            "project_id": pid,
            "total": len(claims),
            "sentences": len(utterances),
            "by_theme": by_theme,
            "claims": [_claim_payload(c) for c in claims],
        },
        formula=(
            "逐句匹配主题词表（app.engine.narrative._THEMES），"
            "命中排除词或风险/否定词的句子不计入"
        ),
    )


@tool(
    name="narrative.consistency",
    version="v1",
    description_cn="把管理层主张与财务事实逐年比对，逐条给出支持/部分支持/冲突/不可比/缺失，并汇总",
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "theme_key": {"type": "string", "description": "只看某一类主题；不填则全部"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "observations": {"type": "array"},
            "counts": {"type": "object"},
            "headline": {"type": "string"},
        },
    },
)
def narrative_consistency(
    ctx: StepContext, project_id: str | None = None, theme_key: str | None = None
) -> ToolOutcome:
    con = ctx.repo.con
    pid, err = _resolve_project(con, project_id)
    if err:
        return ToolOutcome(summary=f"无法读取叙事：{err}", value={"error": err})

    utterances = _load_utterances(con, pid)
    if not utterances:
        return ToolOutcome(
            summary="这个项目还没有 MD&A 正文。先跑：python scripts/parse_mdna.py",
            value={"project_id": pid, "observations": [], "no_text": True},
        )

    claims = find_claims(utterances)
    if theme_key:
        claims = [c for c in claims if c.theme_key == theme_key]

    raw = _rule(con, "narrative.min_rel_change", str(DEFAULT_MIN_REL_CHANGE))
    try:
        min_rel = Decimal(raw)
    except (InvalidOperation, TypeError):
        min_rel = DEFAULT_MIN_REL_CHANGE

    series = _metric_series(con, pid)
    observations = verify_all(claims, series, min_rel_change=min_rel)
    verdict = summarize(observations)

    payload = [
        _obs_payload(
            ob,
            # 只有**判出了方向**的观测才去查数字出处：判成缺失或不可比时，
            # 那一年的数本来就没取到，查了也是空 dict，白跑一次查询。
            _source_of(con, pid, ob.claim.metric.key, ob.claim.verify_period)
            if ob.state not in (MISSING, INCOMPARABLE)
            else {},
        )
        for ob in observations
    ]

    # 按主题分组：评审要看的不是「35 条里 9 条冲突」，
    # 而是「哪一类说法系统性地对不上」——那才是有投资含义的东西。
    themes: list[dict[str, Any]] = []
    for key, theme in THEMES.items():
        rows = [o for o in payload if o["theme_key"] == key]
        if not rows:
            continue
        cnt = {s: sum(1 for r in rows if r["state"] == s) for s in STATE_CN}
        themes.append(
            {
                "theme_key": key,
                "label_cn": theme.label_cn,
                "metric_label": theme.metric.label_cn,
                "basis_cn": theme.basis_cn,
                "total": len(rows),
                "counts": cnt,
            }
        )

    return ToolOutcome(
        summary=f"{verdict.headline}｜{verdict.text.splitlines()[1] if chr(10) in verdict.text else ''}",
        value={
            "project_id": pid,
            "headline": verdict.headline,
            "verdict_text": verdict.text,
            "total": verdict.total,
            "counts": verdict.counts,
            "themes": themes,
            "observations": payload,
            "min_rel_change": str(min_rel),
            "index_note": (
                "诊断指数未出分：公式 I = 50 + 20H + 20C + 5R − 10P − 15Q 已登记在 "
                "rule_config，其中 H（历史兑现度）与 C（当前一致性）由本次观测构成，"
                "R（风险披露变化）、P（模板化惩罚）、Q（财务质量冲突）尚未接入"
                "——Q 依赖 engine/checks.py 的勾稽结果。缺 30 分权重的构成项时出的分"
                "与完整版长得一样，因此不出。"
            ),
        },
        formula=(
            "主张方向（向好）与指标实际方向逐年比对："
            f"相对变动 < {min_rel} 视为未变动；方向相反判冲突"
        ),
    )


# --------------------------------------------------------------- Skill 实现


#: ⚠ 不能与 `facts` / `selfcheck` 的词撞车：路由取**第一个**命中的 Skill
#: （见 `app/skills/__init__.py`），撞了的话后一个永远轮不到，且没有任何提示。
#: 所以这里一律用「叙事」侧的词，不碰「事实」「指标」「趋势」。
_KEYWORDS = ("叙事", "一致性", "管理层", "措辞", "说法", "言行", "兑现", "吹", "承诺")


class NarrativeConsistencySkill:
    key = "narrative"
    name_cn = "叙事一致性"
    description_cn = (
        "从年报管理层讨论与分析中抽取可验证主张，与财务事实逐年比对，"
        "给出支持 / 部分支持 / 冲突 / 不可比 / 缺失的逐条判定。"
    )

    def can_handle(self, request: SkillRequest) -> bool:
        text = request.user_input.strip().lower()
        return any(k in text for k in _KEYWORDS)

    def plan(self, request: SkillRequest) -> list[PlannedStep]:
        pid = request.project_id
        return [
            PlannedStep(name="抽取管理层可验证主张", tool_name="narrative.claims",
                        args={"project_id": pid}),
            PlannedStep(name="与财务事实逐年比对", tool_name="narrative.consistency",
                        args={"project_id": pid}, depends_on=(0,)),
            PlannedStep(name="汇总", depends_on=(1,)),
        ]

    async def run(
        self, step: PlannedStep, ctx: StepContext, prior: dict[int, StepOutcome]
    ) -> StepOutcome:
        """汇总。**不做任何计算**，只组织已经判好的结果。"""
        if step.name != "汇总":
            return StepOutcome(summary=f"未实现的步骤：{step.name}")

        # ⚠ 取值一律经 tool_result()：编排器把工具结果包了一层
        #   （{"result": ..., "formula": ..., "inputs": ...}），
        #   直接读 outcome.value 会拿到那一层，所有 .get() 都是 None。
        v = tool_result(prior.get(1))
        if v.get("error"):
            return StepOutcome(summary=v["error"], value=v)
        if v.get("no_text"):
            return StepOutcome(summary="这个项目还没有 MD&A 正文，先跑 scripts/parse_mdna.py", value=v)

        g = tool_result(prior.get(0))
        lines = [v.get("headline", "")]
        if g.get("total"):
            lines.append(f"  · 从 {g.get('sentences', 0)} 句管理层表述中识别出 {g['total']} 条可验证主张")

        for t in v.get("themes", []):
            c = t["counts"]
            bit = f"支持 {c.get('supported', 0)}"
            if c.get("partial"):
                bit += f" / 部分支持 {c.get('partial')}"
            if c.get("conflicted"):
                bit += f" / 冲突 {c.get('conflicted')}"
            lines.append(f"  · {t['label_cn']}（验 {t['metric_label']}）：{bit}")

        bad = [o for o in v.get("observations", []) if o["state"] == "conflicted"]
        if bad:
            lines.append("  与事实相悖的表述（逐条可点回原句）：")
            for o in bad[:5]:
                lines.append(
                    f"    - {o['source_period']} 年报 p{o['page_no']}「{o['text'][:38]}…」"
                    f" 但{o['metric_label']}{o['actual']}"
                )

        return StepOutcome(
            summary="\n".join(lines) + f"\n\n{v.get('index_note', '')}",
            value=v,
        )


SKILL = NarrativeConsistencySkill()

__all__ = ["NarrativeConsistencySkill", "SKILL"]
