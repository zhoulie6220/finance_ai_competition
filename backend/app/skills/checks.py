"""勾稽校验的取数与落库。

**为什么持久化不放在 engine 里**：`app/engine/` 是「程序负责计算」这条边界的
物理位置，铁律是零 IO、零 sqlite3 导入。校验的算术逻辑全在 `engine/checks.py`，
但「从库里读事实」「把结果写回 fact_check_result」只能由这一层做——它持有
`connect()` 拿到的连接。

这条边界不是洁癖：引擎纯了才能对同一份输入重放出同一个结果，而「同输入必同输出」
是赛事「运行结果可复现」那条要求的实现方式。往引擎里塞一次 `sqlite3.connect()`，
可复现性就没了。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from app.db.repository import current_rule_config_version
from app.engine.checks import (
    ChecksConfig,
    ChecksReport,
    Fact,
    PeriodInput,
    run_checks,
)

# 落库时把 Decimal 序列化成字符串。**绝不用 float**——
# JSON number 是 IEEE 754 双精度，几十亿的金额会掉精度。
MONEY = "0.000001"


class LoadReport:
    """取数过程中的问题清单。

    为什么要单独返回而不是打个日志了事：`value_millions` 这一列在 schema 里
    是 TEXT 且**没有「必须是合法 Decimal」的 CHECK**，所以库里真的可能存在
    解析不出来的值。静默跳过它们会让校验结果看起来「全都通过了」，
    而实际上少查了几项。
    """

    def __init__(self) -> None:
        self.unparsable: list[tuple[str, str, str]] = []   # (fact_id, metric_key, 原值)
        self.duplicates: list[tuple[str, str, int]] = []   # (period, metric_key, 条数)

    @property
    def clean(self) -> bool:
        return not self.unparsable and not self.duplicates

    def warnings(self) -> list[str]:
        out: list[str] = []
        for fact_id, metric_key, raw in self.unparsable:
            out.append(
                f"事实 {fact_id}（{metric_key}）的 value_millions={raw!r} 不是合法数字，"
                f"已跳过——**不计入校验**。请检查解析环节。"
            )
        for period, metric_key, count in self.duplicates:
            out.append(
                f"{period} 年的 {metric_key} 有 {count} 条事实，只取了第一条。"
                f"同一指标同一期间本应只有一行，多行说明口径或期间维度出了问题。"
            )
        return out


def load_periods(
    con: sqlite3.Connection,
    project_id: str,
    *,
    scope: str | None = None,
    report: LoadReport | None = None,
) -> tuple[PeriodInput, ...]:
    """把库里的事实读成引擎的输入结构。

    只读**已验证且可比**的行吗？不——勾稽校验恰恰要检查数据本身，包括那些
    还没进 validated 的行。不可比的行读进来后由引擎按规则排除，并明确报出
    `skipped_incomparable`，而不是在取数阶段悄悄丢掉（丢掉就没人知道它们存在）。
    """
    if scope is None:
        row = con.execute(
            "SELECT base_scope FROM project WHERE project_id = ?", (project_id,)
        ).fetchone()
        scope = row["base_scope"] if row else "consolidated"

    rows = con.execute(
        """
        SELECT fact_id, metric_key, value_millions, period, comparable,
               incomparable_reason
        FROM financial_fact
        WHERE project_id = ? AND scope = ?
        ORDER BY period, metric_key, fact_id
        """,
        (project_id, scope),
    ).fetchall()

    by_period: dict[str, list[Fact]] = {}
    seen: dict[tuple[str, str], int] = {}
    for r in rows:
        raw = r["value_millions"]
        if raw is None or str(raw).strip() == "":
            # 「缺失即无行」——但有行而值为空是另一回事，说明解析出了问题
            if report is not None:
                report.unparsable.append((r["fact_id"], r["metric_key"], repr(raw)))
            continue
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            if report is not None:
                report.unparsable.append((r["fact_id"], r["metric_key"], str(raw)))
            continue

        key = (r["period"], r["metric_key"])
        seen[key] = seen.get(key, 0) + 1

        by_period.setdefault(r["period"], []).append(
            Fact(
                fact_id=r["fact_id"],
                metric_key=r["metric_key"],
                value=value,
                comparable=bool(r["comparable"]),
                incomparable_reason=r["incomparable_reason"],
            )
        )

    if report is not None:
        report.duplicates.extend(
            (period, metric, count)
            for (period, metric), count in sorted(seen.items())
            if count > 1
        )

    return tuple(
        PeriodInput(period=period, scope=scope, facts=tuple(facts))
        for period, facts in sorted(by_period.items())
    )


def config_from_rules(con: sqlite3.Connection) -> ChecksConfig:
    """从 rule_config 读容差。

    **刻意不从代码默认值跑**：页面上的「查看/修改/恢复默认」改的就是
    rule_config，如果引擎还用代码里的默认值，那么改完参数之后
    显示的口径和实际生效的口径就对不上了——而且不会有任何报错。
    """
    rows = {
        r["key"]: r["value"]
        for r in con.execute(
            "SELECT key, value FROM rule_config WHERE key LIKE 'check.%'"
        )
    }
    kwargs: dict[str, Decimal] = {}
    if "check.balance_tolerance" in rows:
        kwargs["balance_tolerance"] = Decimal(rows["check.balance_tolerance"])
    return ChecksConfig(**kwargs) if kwargs else ChecksConfig()


def run_project_checks(
    con: sqlite3.Connection,
    project_id: str,
    *,
    scope: str | None = None,
) -> tuple[ChecksReport, LoadReport]:
    """取数 → 跑引擎。不落库。"""
    load = LoadReport()
    periods = load_periods(con, project_id, scope=scope, report=load)
    report = run_checks(
        periods,
        project_id=project_id,
        cfg=config_from_rules(con),
    )
    return report, load


def persist(
    con: sqlite3.Connection,
    report: ChecksReport,
    *,
    now: str,
) -> int:
    """把校验结论写进 `fact_check_result`，返回写入行数。

    校验结果是**派生数据**，随时能由事实重算，所以这里整批替换而不是增量追加：
    增量追加会在重跑之后留下上一轮的旧结论，页面上新旧混排且不报错。
    删除与写入在同一个事务里，中途失败整体回滚。

    `check_id` 是确定性的（项目+规则+期间+口径的哈希），所以重跑写回同一批 id，
    不会因为换了个随机数就让历史引用失效。
    """
    with con:
        con.execute(
            "DELETE FROM fact_check_result WHERE project_id = ?",
            (report.project_id,),
        )
        rows = [
            (
                _check_id(report.project_id, o),
                report.project_id,
                o.period,
                o.scope,
                o.rule_key,
                o.severity,
                o.status,
                _money(o.lhs),
                _money(o.rhs),
                _money(o.diff),
                _money(o.tolerance),
                o.formula,
                json.dumps(list(o.inputs), ensure_ascii=False),
                o.message,
                o.suggestion,
                now,
            )
            for o in report.outcomes
        ]
        con.executemany(
            "INSERT INTO fact_check_result (check_id, project_id, period, scope,"
            " rule_key, severity, status, lhs, rhs, diff, tolerance, formula,"
            " input_facts, message, suggestion, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def run_and_persist(
    con: sqlite3.Connection,
    project_id: str,
    *,
    now: str,
    scope: str | None = None,
) -> tuple[ChecksReport, LoadReport, int]:
    """接口层用的一步到位：取数 → 校验 → 落库。

    每次调用都重算而不是读缓存：数据可能有变，而读一份过期结论
    比重新算一遍危险得多——后者慢，前者错。
    """
    report, load = run_project_checks(con, project_id, scope=scope)
    written = persist(con, report, now=now)
    return report, load, written


def load_results(
    con: sqlite3.Connection, project_id: str
) -> list[dict[str, Any]]:
    """读回落库的结果，供「与上次运行对照」。"""
    rows = con.execute(
        "SELECT check_id, period, scope, rule_key, severity, status,"
        " lhs, rhs, diff, tolerance, formula, input_facts, message, suggestion,"
        " created_at"
        " FROM fact_check_result WHERE project_id = ?"
        " ORDER BY period, rule_key",
        (project_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        try:
            item["input_facts"] = json.loads(r["input_facts"])
        except (ValueError, TypeError):
            item["input_facts"] = []
        out.append(item)
    return out


def report_to_json(report: ChecksReport, load: LoadReport) -> dict[str, Any]:
    """校验结果出网形状。金额一律是字符串。"""
    return {
        "project_id": report.project_id,
        "scope": report.scope,
        "method_version": report.method_version,
        "summary": {
            # 覆盖率与不平数**必须一起给**：只说「全部通过」会把
            # 「大部分项目根本没查」读成「什么都对得上」
            "total": report.total_count,
            "evaluable": report.evaluable_count,
            "passed": sum(1 for o in report.outcomes if o.status == "passed"),
            "failed": len(report.failures),
            "hard_failed": len(report.hard_failures),
            "soft_failed": len(report.soft_failures),
            "skipped_missing_data": sum(
                1 for o in report.outcomes if o.status == "skipped_missing_data"
            ),
            "skipped_incomparable": sum(
                1 for o in report.outcomes if o.status == "skipped_incomparable"
            ),
            "sheet_ok": report.sheet_ok,
            "coverage_line": report.coverage_line(),
        },
        "results": [
            {
                "rule_key": o.rule_key,
                "period": o.period,
                "scope": o.scope,
                "status": o.status,
                "severity": o.severity,
                "lhs": _money(o.lhs),
                "rhs": _money(o.rhs),
                "diff": _money(o.diff),
                "tolerance": _money(o.tolerance),
                "formula": o.formula,
                "inputs": list(o.inputs),
                "message": o.message,
                "suggestion": o.suggestion,
            }
            for o in report.outcomes
        ],
        "warnings": load.warnings(),
    }


# ---------------------------------------------------------------- 内部


def _money(value: Decimal | None) -> str | None:
    """Decimal → 字符串，**绝不过 float**。None 原样返回（表示「未产出」）。"""
    if value is None:
        return None
    return str(value.quantize(Decimal(MONEY)))


def _check_id(project_id: str, outcome: Any) -> str:
    import hashlib

    digest = hashlib.sha256(
        f"{project_id}\x00{outcome.rule_key}\x00{outcome.period}\x00{outcome.scope}"
        .encode("utf-8")
    ).hexdigest()
    return "chk-" + digest[:16]


def rule_config_version(con: sqlite3.Connection) -> int:
    return current_rule_config_version(con)
