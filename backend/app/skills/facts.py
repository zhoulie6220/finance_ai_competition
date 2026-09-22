"""Skill 1 · 财务事实（读盘）。

读 `v_fact_verified`，把已落库的财务事实铺成「指标 × 期间」的样子，并算出同比。
这是上游（PDF 解析）还没到位时，**前端唯一能看到真实业务数据的地方**——
配合 `scripts/seed_demo.py` 的演示数据，界面开发和验收可以先跑起来。

三件事刻意做对了：

1. **只读 `v_fact_verified`**，不读 `financial_fact`。那个视图只暴露
   `status='validated' AND comparable=1` 的行——不可信的数据在 SQL 层就进不来，
   而不是靠这个文件记得加 WHERE。
2. **计算全部走 `app.engine.ratios`**：本文件里没有一行除法。
   引擎是纯函数，同输入必同输出；工具只负责取数与组织文字。
3. **拒绝照原样呈现**。引擎说「基期为负，应看扭亏而非增长率」，
   摘要里就得是这句话，而不是一个数字后面缀个星号——
   带星号的数字会被人直接拿去用。
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.engine.ratios import gross_margin, yoy_growth
from app.skills.base import PlannedStep, SkillRequest, StepOutcome, tool_result
from app.tools.registry import ToolOutcome, tool

if TYPE_CHECKING:
    from app.agents.orchestrator import StepContext

#: 没有指定项目时的兜底：**只有恰好一个项目才自动用它**。
#: 有多个就报错并列出候选——随便挑一个的后果是用户看着一家公司的页面，
#: 以为在看另一家。
def _resolve_project(con, project_id: str | None) -> tuple[str | None, str | None]:
    """返回 (project_id, 错误说明)。"""
    if project_id:
        row = con.execute(
            "SELECT name FROM project WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            return None, f"项目不存在：{project_id}"
        return project_id, None

    rows = con.execute("SELECT project_id, name FROM project ORDER BY created_at").fetchall()
    if not rows:
        return None, "库里还没有任何项目。灌入演示数据：python scripts/seed_demo.py"
    if len(rows) > 1:
        names = "、".join(r["name"] for r in rows)
        return None, f"有 {len(rows)} 个项目，请在请求里指定 project_id：{names}"
    return rows[0]["project_id"], None


def _fmt(value: Decimal | str) -> str:
    """把百万元数排成人看的样子。"""
    d = value if isinstance(value, Decimal) else Decimal(value)
    return f"{d:,.0f}"


def _read_series(
    con, project_id: str, metric_key: str
) -> tuple[list[dict[str, str]], str, str]:
    """取一个指标的逐年序列。返回 (点列, 中文名, 单位)。"""
    rows = con.execute(
        "SELECT f.period, f.value_millions, f.unit, d.label_cn"
        " FROM v_fact_verified f"
        " JOIN metric_definition d ON d.metric_key = f.metric_key"
        " WHERE f.project_id=? AND f.metric_key=?"
        "   AND f.period_kind='current'"      # 时点余额不参与同比
        "   AND f.period_start IS NULL"       # 只取年度数，排除半年报
        " GROUP BY f.period"                  # 同一期间多来源时取一条
        " ORDER BY f.period",
        (project_id, metric_key),
    ).fetchall()
    label = rows[0]["label_cn"] if rows else metric_key
    unit = rows[0]["unit"] if rows else "百万元"
    return [{"period": r["period"], "value": r["value_millions"]} for r in rows], label, unit


def _with_yoy(points: list[dict[str, str]]) -> list[dict[str, Any]]:
    """逐年算同比。**算不出来的年份把拒绝理由原样带上**，不填 None 了事。"""
    out: list[dict[str, Any]] = []
    for i, p in enumerate(points):
        item: dict[str, Any] = dict(p)
        if i == 0:
            item["yoy"] = None
            item["yoy_note"] = "首年，无上期"
            out.append(item)
            continue
        prev = points[i - 1]
        r = yoy_growth(
            Decimal(prev["value"]), Decimal(p["value"]),
            prev_period=prev["period"], curr_period=p["period"],
        )
        item["yoy"] = str(r.value) if r.ok else None
        item["yoy_note"] = None if r.ok else r.refused
        item["yoy_formula"] = r.formula
        out.append(item)
    return out


# --------------------------------------------------------------- Tool 实现


@tool(
    name="facts.coverage",
    version="v1",
    description_cn="统计项目里已验证的财务事实覆盖了哪些年份、多少个指标，并指出缺口",
    input_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string", "description": "项目 id；不填则自动选用唯一的项目"}},
    },
    output_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "project_name": {"type": "string"},
            "years": {"type": "array"},
            "metrics": {"type": "integer"},
            "facts": {"type": "integer"},
        },
    },
)
def facts_coverage(ctx: StepContext, project_id: str | None = None) -> ToolOutcome:
    con = ctx.repo.con
    pid, err = _resolve_project(con, project_id)
    if err:
        return ToolOutcome(summary=f"无法读取财务事实：{err}", value={"error": err})

    row = con.execute(
        "SELECT name, company_name, stock_code FROM project WHERE project_id=?", (pid,)
    ).fetchone()
    years = [
        r["period"] for r in con.execute(
            "SELECT DISTINCT period FROM v_fact_verified"
            " WHERE project_id=? AND period_kind='current' ORDER BY period",
            (pid,),
        )
    ]
    n_metrics, n_facts = con.execute(
        "SELECT COUNT(DISTINCT metric_key), COUNT(*) FROM v_fact_verified WHERE project_id=?",
        (pid,),
    ).fetchone()

    return ToolOutcome(
        summary=(
            f"{row['name']}：已验证事实 {n_facts} 条，覆盖 {n_metrics} 个指标、"
            f"{len(years)} 个年度（{years[0]}–{years[-1]}）"
            if years else f"{row['name']}：还没有已验证的财务事实"
        ),
        value={
            "project_id": pid, "project_name": row["name"],
            "company_name": row["company_name"], "stock_code": row["stock_code"],
            "years": years, "metrics": n_metrics, "facts": n_facts,
        },
        formula="SELECT ... FROM v_fact_verified（只含 validated 且可比的行情）",
    )


@tool(
    name="facts.series",
    version="v1",
    description_cn="取一个财务指标的逐年序列，并逐年计算同比（增长率由引擎计算，不可比时拒绝出数）",
    input_schema={
        "type": "object",
        "properties": {
            "metric_key": {"type": "string", "description": "指标键，如 revenue / net_income / cfo"},
            "project_id": {"type": "string"},
        },
        "required": ["metric_key"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "metric_key": {"type": "string"},
            "points": {"type": "array"},
        },
    },
)
def facts_series(
    ctx: StepContext, metric_key: str, project_id: str | None = None
) -> ToolOutcome:
    con = ctx.repo.con
    pid, err = _resolve_project(con, project_id)
    if err:
        return ToolOutcome(summary=f"无法读取财务事实：{err}", value={"error": err})

    points, label, unit = _read_series(con, pid, metric_key)
    if not points:
        # ⚠ 「这个指标没数据」与「公司没披露这一项」在库里长得一样，
        #   但用户要做的事完全不同，所以要把 metric_key 原样报出来。
        return ToolOutcome(
            summary=f"没有找到指标 {metric_key} 的年度数据（可能是未披露，也可能是键名不对）",
            value={"metric_key": metric_key, "points": [], "missing": True},
        )

    series = _with_yoy(points)
    first, last = points[0], points[-1]
    latest_yoy = next((s for s in reversed(series) if s["yoy"]), None)
    tail = (
        f"；{last['period']} 同比 {Decimal(latest_yoy['yoy']):+.1%}"
        if latest_yoy else ""
    )
    refused = [s for s in series if s["yoy_note"] and s["period"] != first["period"]]

    return ToolOutcome(
        summary=(
            f"{label} {first['period']}–{last['period']}："
            f"{_fmt(first['value'])} → {_fmt(last['value'])} {unit}{tail}"
            + (f"（{len(refused)} 个年度同比不可比，已拒绝出数）" if refused else "")
        ),
        value={
            "metric_key": metric_key, "label_cn": label, "unit": unit,
            "project_id": pid, "points": series,
            "refused_years": [s["period"] for s in refused],
        },
        formula="同比 = (本期 − 上期) / |上期|，逐年在 app.engine.ratios.yoy_growth 计算",
        inputs={s["period"]: s["value"] for s in points},
    )


@tool(
    name="facts.margin",
    version="v1",
    description_cn="计算逐年毛利率，并标出不可直接比较的年度",
    input_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
    },
    output_schema={"type": "object", "properties": {"points": {"type": "array"}}},
)
def facts_margin(ctx: StepContext, project_id: str | None = None) -> ToolOutcome:
    """毛利率。用来在界面上展示「图上的每个点都能点回它怎么算的」。"""
    con = ctx.repo.con
    pid, err = _resolve_project(con, project_id)
    if err:
        return ToolOutcome(summary=f"无法读取财务事实：{err}", value={"error": err})

    gross, g_label, _ = _read_series(con, pid, "gross_profit")
    rev, _, _ = _read_series(con, pid, "revenue")
    rev_by_year = {p["period"]: p["value"] for p in rev}

    points: list[dict[str, Any]] = []
    for p in gross:
        r = gross_margin(Decimal(p["value"]), (
            Decimal(rev_by_year[p["period"]]) if p["period"] in rev_by_year else None
        ))
        points.append({
            "period": p["period"],
            "margin": str(r.value) if r.ok else None,
            "note": None if r.ok else r.refused,
            "formula": r.formula,
            "inputs": r.inputs,
        })

    usable = [p for p in points if p["margin"]]
    peak = max(usable, key=lambda p: Decimal(p["margin"])) if usable else None
    trough = min(usable, key=lambda p: Decimal(p["margin"])) if usable else None
    summary = (
        f"{g_label}率 {len(usable)} 个年度可比"
        + (f"，最高 {peak['period']} {Decimal(peak['margin']):.1f}%"
           f"、最低 {trough['period']} {Decimal(trough['margin']):.1f}%" if peak else "")
        + (f"（{len(points) - len(usable)} 个年度因缺值拒绝出数）"
           if len(points) != len(usable) else "")
    )
    return ToolOutcome(
        summary=summary,
        value={"label_cn": f"{g_label}率", "unit": "%", "points": points},
        formula="毛利率 = 毛利 / 营业收入 × 100，逐年调用 app.engine.ratios.gross_margin",
    )


# --------------------------------------------------------------- Skill 实现

_KEYWORDS = ("财务事实", "事实", "指标", "趋势", "营收", "收入", "利润", "毛利率", "同比")


class FinancialFactsSkill:
    key = "facts"
    name_cn = "财务事实"
    description_cn = "读取已落库的财务事实，出「指标 × 期间」的序列与同比。依赖年报解析结果。"

    def can_handle(self, request: SkillRequest) -> bool:
        text = request.user_input.strip().lower()
        return any(k in text for k in _KEYWORDS)

    def plan(self, request: SkillRequest) -> list[PlannedStep]:
        pid = request.project_id
        return [
            PlannedStep(name="查看数据覆盖情况", tool_name="facts.coverage",
                        args={"project_id": pid}),
            PlannedStep(name="取营业收入十年序列", tool_name="facts.series",
                        args={"metric_key": "revenue", "project_id": pid}, depends_on=(0,)),
            PlannedStep(name="取净利润十年序列", tool_name="facts.series",
                        args={"metric_key": "net_income", "project_id": pid}, depends_on=(0,)),
            PlannedStep(name="计算逐年毛利率", tool_name="facts.margin",
                        args={"project_id": pid}, depends_on=(0,)),
            PlannedStep(name="汇总", depends_on=(1, 2, 3)),
        ]

    async def run(
        self, step: PlannedStep, ctx: StepContext, prior: dict[int, StepOutcome]
    ) -> StepOutcome:
        """最后一步：把前面的结果组织成一段话。

        **这里不做任何计算**，只组织已经算好的数字——去 `prior` 里取的是
        结构化结果，不是摘要字符串（改一个措辞就会让解析静默失效）。
        """
        if step.name != "汇总":
            return StepOutcome(summary=f"未实现的步骤：{step.name}")

        # ⚠ 取值一律经 tool_result()：编排器把工具结果包了一层
        #   （{"result": ..., "formula": ..., "inputs": ...}），
        #   直接读 outcome.value 会拿到那一层，所有 .get("points") 都是 None。
        cov = tool_result(prior.get(0))
        if cov.get("error"):
            return StepOutcome(summary=cov["error"], value=cov)

        lines = [f"{cov.get('project_name', '（未取到项目）')} 的财务事实："]
        for idx, label in ((1, "营业收入"), (2, "净利润")):
            out = prior.get(idx)
            if tool_result(out).get("points"):
                lines.append(f"  · {out.summary}")
            else:
                lines.append(f"  · {label}：无数据")

        margin, refused = prior.get(3), []
        m = tool_result(margin)
        if m.get("points"):
            lines.append(f"  · {margin.summary}")
            refused = [p["period"] for p in m["points"] if p.get("note")]

        note = f"\n（{len(refused)} 个年度的毛利率不可比：{'、'.join(refused)}）" if refused else ""

        return StepOutcome(
            summary="\n".join(lines) + note,
            value={
                "coverage": cov,
                "series": {
                    k: tool_result(prior.get(i)) for k, i in (("revenue", 1), ("net_income", 2))
                },
                "margin": m or None,
            },
        )


SKILL = FinancialFactsSkill()

__all__ = ["FinancialFactsSkill", "SKILL"]
