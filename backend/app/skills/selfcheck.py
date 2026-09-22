"""Skill 0 · 系统自检。

**这是第一个落地的 Skill，它是真的在干活，不是占位。**

选它做第一条链路，是因为它能把编排、Tool 留痕、SSE 时间线三件事一次打通，
而且不需要等年报 PDF 到位。它检查的三件事都是真实存在的：

  1. 数据库结构与种子数据是否完整
  2. 字段字典是否通过全部规则（`app/db/dictionary.py::validate`）
  3. `rule_config` 里页面上「可查看/可修改」的参数，与引擎实际用的默认值是否一致

第 3 条尤其值得跑：**两处不一致时不会有任何提示**。页面上写着周期落差是 2%，
算了半天用的是别的数——这正是本项目最怕的那一类缺陷。
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from app.schemas.enums import ParamTier
from app.skills.base import PlannedStep, SkillRequest, StepOutcome, tool_result
from app.tools.registry import ToolOutcome, tool

if TYPE_CHECKING:
    from app.agents.orchestrator import StepContext


# --------------------------------------------------------------- Tool 实现


@tool(
    name="system.db_stats",
    version="v1",
    description_cn="统计数据库的表、视图、字段字典与规则参数数量，确认种子数据装载完整",
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {
            "tables": {"type": "integer"},
            "views": {"type": "integer"},
            "metrics": {"type": "integer"},
            "rules": {"type": "integer"},
        },
    },
)
def db_stats(ctx: StepContext) -> ToolOutcome:
    con = ctx.repo.con
    tables = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts_%'"
    ).fetchone()[0]
    views = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='view'"
    ).fetchone()[0]
    metrics = con.execute("SELECT COUNT(*) FROM metric_definition").fetchone()[0]
    rules = con.execute("SELECT COUNT(*) FROM rule_config").fetchone()[0]
    legacy = con.execute("SELECT COUNT(*) FROM metric_key_migration").fetchone()[0]

    return ToolOutcome(
        summary=f"表 {tables} 张、视图 {views} 个、字段字典 {metrics} 项、"
                f"规则参数 {rules} 条、旧键名映射 {legacy} 条",
        value={"tables": tables, "views": views, "metrics": metrics,
               "rules": rules, "legacy_keys": legacy},
        formula="SELECT COUNT(*) FROM sqlite_master / metric_definition / rule_config",
    )


@tool(
    name="system.dictionary_check",
    version="v1",
    description_cn="跑字段字典的全部校验规则，返回问题清单（空表示通过）",
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {"problems": {"type": "array"}, "passed": {"type": "boolean"}},
    },
)
def dictionary_check(ctx: StepContext) -> ToolOutcome:
    from app.db.dictionary import validate

    problems = validate(ctx.repo.con)
    return ToolOutcome(
        summary=("字段字典通过全部规则" if not problems
                 else f"字段字典有 {len(problems)} 个问题"),
        value={"passed": not problems, "problems": problems},
        formula="app.db.dictionary.validate(con)",
    )


@tool(
    name="system.rule_config_check",
    version="v1",
    description_cn="检查规则参数的分级分布，并核对配置值与引擎默认值是否一致",
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {"by_tier": {"type": "object"}, "mismatches": {"type": "array"}},
    },
)
def rule_config_check(ctx: StepContext) -> ToolOutcome:
    """★ 配置与引擎默认值的对拍。

    页面上可改的参数和引擎实际用的默认值，是**两份**东西。它们一旦分叉，
    页面上显示的口径就和实际计算的口径不一致，而且两边都不会报错。
    这里把已知的几组逐一比对。
    """
    from app.engine.normalization import NormalizationConfig

    con = ctx.repo.con
    by_tier = Counter(
        r["tier"] for r in con.execute("SELECT tier FROM rule_config")
    )
    bad = [
        k for k in by_tier
        if k not in {t.value for t in ParamTier}
    ]

    cfg = NormalizationConfig()
    values = {
        r["key"]: r["value"]
        for r in con.execute("SELECT key, value FROM rule_config WHERE industry=''")
    }
    # (rule_config 键, 引擎默认值, 中文名)
    pairs: list[tuple[str, Any, str]] = [
        ("normalization.preferred_years", cfg.preferred_years, "主窗口年数"),
        ("normalization.fallback_years", cfg.fallback_years, "回退窗口年数"),
        ("normalization.min_comparable_years", cfg.min_comparable_years, "主窗口最少可比年度"),
        ("normalization.min_comparable_years_fallback",
         cfg.min_comparable_years_fallback, "回退窗口最少可比年度"),
        ("normalization.phase_min_spread", cfg.min_cycle_amplitude, "高低盈利阶段最低落差"),
        ("normalization.ebit_crosscheck_tolerance",
         cfg.crosscheck_tolerance, "EBIT 两法差异容差"),
    ]
    mismatches: list[dict[str, Any]] = []
    for key, engine_value, label in pairs:
        raw = values.get(key)
        if raw is None:
            mismatches.append({"key": key, "label": label,
                               "config": None, "engine": str(engine_value),
                               "reason": "rule_config 里没有这一行"})
            continue
        # 用 Decimal 比较而不是浮点：0.02 与 '0.02' 在浮点下可能不等
        from decimal import Decimal, InvalidOperation

        try:
            if Decimal(raw) != Decimal(str(engine_value)):
                mismatches.append({"key": key, "label": label, "config": raw,
                                   "engine": str(engine_value), "reason": "取值不一致"})
        except InvalidOperation:
            mismatches.append({"key": key, "label": label, "config": raw,
                               "engine": str(engine_value), "reason": "配置值不是数字"})

    tier_cn = "、".join(f"{k} {v} 条" for k, v in sorted(by_tier.items()))
    summary = (
        f"规则参数分级：{tier_cn}；"
        + ("配置值与引擎默认值一致" if not mismatches
           else f"⚠ {len(mismatches)} 项与引擎默认值不一致")
    )
    return ToolOutcome(
        summary=summary,
        value={"by_tier": dict(by_tier), "mismatches": mismatches,
               "unknown_tiers": bad},
        formula="NormalizationConfig() 与 rule_config(industry='') 逐项比对",
    )


# --------------------------------------------------------------- Skill 实现

#: 关键词路由。规则优先于 LLM —— 反过来会让简单请求也走一次模型调用，又慢又不可复现。
_KEYWORDS = ("自检", "健康检查", "health", "系统检查", "体检")


class SelfCheckSkill:
    key = "selfcheck"
    name_cn = "系统自检"
    description_cn = "检查数据库结构、字段字典与服务该参数的一致性。不依赖年报材料，可随时运行。"

    def can_handle(self, request: SkillRequest) -> bool:
        text = request.user_input.strip().lower()
        return any(k in text for k in _KEYWORDS)

    def plan(self, request: SkillRequest) -> list[PlannedStep]:
        return [
            PlannedStep(name="检查数据库结构与种子数据", tool_name="system.db_stats"),
            PlannedStep(name="跑字段字典的全部校验规则",
                        tool_name="system.dictionary_check", depends_on=(0,)),
            PlannedStep(name="核对规则参数与引擎默认值",
                        tool_name="system.rule_config_check", depends_on=(0,)),
            PlannedStep(name="汇总自检结果", depends_on=(1, 2)),
        ]

    async def run(
        self, step: PlannedStep, ctx: StepContext, prior: dict[int, StepOutcome]
    ) -> StepOutcome:
        """最后一步由 Skill 自己处理：汇总前三步，不调工具。

        汇总需要看到前面步骤的结果，而工具之间是彼此独立的——所以「汇总」
        天然属于 Skill 而不是 Tool。

        `prior` 按**计划下标**索引，与 `plan()` 返回的顺序一一对应：
        下标 1 是字典检查，下标 2 是规则参数检查。
        读的是结构化结果而不是摘要字符串 —— 去摘要里找「问题」两个字，
        改一个措辞就会让汇总静默失效。
        """
        if step.name != "汇总自检结果":
            return StepOutcome(summary=f"未实现的步骤：{step.name}")

        problems: list[str] = []
        passed = True

        # ⚠ 一律经 tool_result() 取值。直接读 `outcome.value` 会拿到编排器包的那一层，
        #   `value.get("passed")` 恒为 None，于是**检查失败也会显示「自检通过」**。
        d = tool_result(prior.get(1))
        if d.get("passed") is False:
            passed = False
            problems.extend(d.get("problems") or [])

        r = tool_result(prior.get(2))
        for m in r.get("mismatches") or []:
            passed = False
            problems.append(
                f"{m.get('label') or m.get('key')}：配置值 {m.get('config')!r} "
                f"与引擎默认值 {m.get('engine')!r} 不一致（{m.get('reason')}）"
            )
        if r.get("unknown_tiers"):
            passed = False
            problems.append(f"非法的参数分级：{r['unknown_tiers']}")

        if not passed:
            # 摘要里带上具体问题，不要只说「N 项需要处理」：
            # 时间线上显示的就是这一行，只说数量的话用户还得再点一层才知道
            # 坏在哪——而这一步的全部意义就是让人立刻看到问题。
            head = problems[0][:80]
            more = f"…等 {len(problems)} 项" if len(problems) > 1 else ""
            return StepOutcome(
                summary=f"⚠ 自检发现 {len(problems)} 项需要处理：{head}{more}",
                value={"passed": False, "problems": problems},
            )

        db_out = prior.get(0)
        summary = db_out.summary if db_out else "（未取到数据库统计）"
        return StepOutcome(
            summary=f"自检通过 · {summary}",
            value={"passed": True, "problems": []},
        )


SKILL = SelfCheckSkill()

__all__ = ["SelfCheckSkill", "SKILL"]
