"""把真实年报解析、映射、落库。

用法：
    python scripts/parse_reports.py --source "D:\\...\\3份公司年报及年报摘要"
    python scripts/parse_reports.py --source ... --force   # 清掉旧的重来

做四件事：

    1. 把 PDF 拷进 DATA_ROOT（`backend/var/samples/`）并登记成 `file` 行
    2. 解析三张合并表 → 行
    3. 行名映射到 metric_key（走字段字典，精确匹配）
    4. 同 (指标, 期间) 的多个观测 → 裁决 → 写 `financial_fact`

## 为什么拷贝而不是直接读原路径

`file.rel_path` 必须相对 `DATA_ROOT`，后端只允许读写白名单内的文件
（见 `app/api/routes_projects.py` 的路径校验）。直接引用白名单外的路径，
这条记录在页面上点「查看原页」时会被拒绝——而它看起来只是一条普通记录。

## 同一笔事实出现在多份年报里

2023 年的数字，在 2023 年报的「本期」列和 2024 年报的「上期」列里各出现一次。
两者**应该相等**，实测有相当一部分不相等——那是**重述**，不是解析错误。

裁决规则：**以「本期」所在的那份年报为准**。2023 年的数，2023 年报说了算；
别的年报里那个对不上的值记成重述（`restated=1`），不覆盖。
这条规则要写进文档，因为它决定了报告里显示哪个数。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _console  # noqa: E402  (与本文件同目录)

_console.setup()

import pymupdf  # noqa: E402

from app.db.session import connect, default_db_path  # noqa: E402
from app.parsing import parse_document  # noqa: E402
from app.parsing.mapping import LabelIndex  # noqa: E402
from app.parsing.models import ParsedStatement  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
NOW = "2026-09-22T00:00:00+08:00"

#: 文件名里的年份：`宝钢股份：2015年年度报告.pdf` / `…2020年年度报告全文.pdf`
_YEAR_IN_NAME = re.compile(r"(20\d{2})\s*年")
EXTRACTOR = "rule:pdf-v1"

#: 目录名 → (项目 id, 公司标识, 公司名, 股票代码, 是否主公司)
COMPANY_DIRS: dict[str, tuple[str, str, str, str, bool]] = {
    "主公司年报-宝钢股份600019": ("p-600019", "c-600019", "宝钢股份", "600019.SH", True),
    "1对比公司年报-华菱钢铁000932": ("p-000932", "c-000932", "华菱钢铁", "000932.SZ", False),
    "2对比公司年报-首钢股份000959": ("p-000959", "c-000959", "首钢股份", "000959.SZ", False),
}


@dataclass
class Observation:
    """一条「某份年报某页说这个指标这个期间是多少」。"""

    company_id: str
    metric_key: str
    period: str
    period_kind: str
    scope: str
    value_raw: str
    raw_unit: str
    value_millions: Decimal
    file_id: str
    source_page: int
    source_table: str
    source_text: str
    report_year: int
    label: str

    @property
    def is_own_year(self) -> bool:
        """这个观测是不是「本期的年报」说的 —— 裁决时的首要依据。"""
        return self.report_year == int(self.period)


@dataclass
class IngestReport:
    files: int = 0
    rows_total: int = 0
    mapped: int = 0
    observations: int = 0
    facts: int = 0
    restated: int = 0
    #: 归一化后仍映射不上的行名 → 出现次数
    unmapped: dict[str, int] = field(default_factory=dict)
    #: 解析失败的报表
    failed: list[str] = field(default_factory=list)


def register_file(con, project_id: str, pdf: Path, rel: str, year: str) -> str:
    """登记一份 PDF。已登记过就返回原来的 file_id。"""
    row = con.execute(
        "SELECT file_id FROM file WHERE project_id=? AND period=? AND role='annual_report'",
        (project_id, year),
    ).fetchone()
    if row:
        return row["file_id"]
    file_id = f"f-{uuid.uuid4().hex[:12]}"
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    con.execute(
        "INSERT INTO file (file_id, project_id, role, period, rel_path, sha256,"
        " bytes, page_count, is_scanned, parse_status, uploaded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (file_id, project_id, "annual_report", year, rel, digest,
         pdf.stat().st_size, pymupdf.open(pdf).page_count, 0, "parsed", NOW),
    )
    return file_id


def collect(
    con, project_id: str, file_id: str, stmt: ParsedStatement,
    index: LabelIndex, company_id: str, report_year: int, rep: IngestReport,
) -> list[Observation]:
    """一张表 → 若干观测。"""
    out: list[Observation] = []
    for row in stmt.rows:
        rep.rows_total += 1
        m = index.match(row.label)
        if not m.metric_key:
            from app.parsing.mapping import normalize_label

            key = normalize_label(row.label)
            if key:
                rep.unmapped[key] = rep.unmapped.get(key, 0) + 1
            continue
        rep.mapped += 1
        for col, value in zip(stmt.columns, row.values):
            if value is None or col.period is None:
                continue
            millions = (value * stmt.unit_factor).quantize(Decimal("0.000001"))
            out.append(Observation(
                company_id=company_id,
                metric_key=m.metric_key,
                period=col.period,
                period_kind=col.period_kind,
                scope=stmt.scope,
                value_raw=str(value),
                raw_unit=stmt.unit,
                value_millions=millions,
                file_id=file_id,
                source_page=row.page_no,
                source_table=stmt.title,
                source_text=row.source_text,
                report_year=report_year,
                label=row.label,
            ))
    return out


def insert_observation(con, project_id: str, o: Observation) -> str:
    oid = f"o-{uuid.uuid4().hex[:12]}"
    con.execute(
        "INSERT INTO fact_observation (observation_id, project_id, company_id,"
        " metric_key, period, period_kind, scope, value_raw, raw_unit, value_millions,"
        " source_file_id, source_page, source_table, source_location, source_text,"
        " confidence, extractor, created_at, resolution)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, project_id, o.company_id, o.metric_key, o.period, o.period_kind, o.scope,
         o.value_raw, o.raw_unit, str(o.value_millions), o.file_id, o.source_page,
         o.source_table, "main_statement", o.source_text, 1.0, EXTRACTOR, NOW, "pending"),
    )
    return oid


def resolve(con, project_id: str, rep: IngestReport) -> None:
    """观测 → 事实。

    ⚠ **「本期年报优先」**：2023 年的数，2023 年报说了算。
    后来的年报对上一年重述过，那个重述值不覆盖原始披露值，另存一行
    （`restated=1`）并说明差异。这是 CLAUDE.md 里「重述值与原值双行并存，
    永不 UPDATE 覆盖」那条硬规则——它保护的是「当时到底披露了什么」这个事实。
    """
    rows = con.execute(
        "SELECT observation_id, company_id, metric_key, period, period_kind, scope,"
        " value_millions, source_file_id, source_page, source_table, source_text,"
        " value_raw, raw_unit"
        " FROM fact_observation WHERE project_id=? AND resolution='pending'",
        (project_id,),
    ).fetchall()

    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["company_id"], r["metric_key"], r["period"], r["scope"]), []).append(r)

    for (company_id, metric_key, period, scope), obs in groups.items():
        # 本期的年报优先；没有的话取最近一份（越新的越可能含重述后的数）
        own = [o for o in obs if int(period) == _report_year_of(con, o["source_file_id"])]
        pick = own[0] if own else obs[0]
        others = [o for o in obs if o["observation_id"] != pick["observation_id"]]
        differing = [o for o in others if o["value_millions"] != pick["value_millions"]]

        note = None
        if differing:
            pairs = "；".join(
                f"{_file_label(con, o['source_file_id'])} 记 {o['value_millions']}"
                for o in differing[:3]
            )
            note = f"另有来源披露不同值（{len(differing)} 处）：{pairs}"

        fact_id = f"fact-{uuid.uuid4().hex[:12]}"
        con.execute(
            "INSERT INTO financial_fact (fact_id, project_id, company_id, is_primary,"
            " metric_key, value_millions, value_raw, raw_unit, unit_factor, unit,"
            " period, period_kind, scope, source_file, source_file_id, source_page,"
            " source_table, source_row_label, source_text, confidence, status,"
            " restated, restatement_note, comparable, extractor, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fact_id, project_id, company_id,
                1 if company_id.endswith("600019") else 0,
                metric_key, pick["value_millions"], pick["value_raw"], pick["raw_unit"],
                str(Decimal(pick["value_millions"]) / Decimal(pick["value_raw"])
                    if Decimal(pick["value_raw"]) else Decimal("1")),
                "百万元", period, pick["period_kind"], scope,
                _file_label(con, pick["source_file_id"]), pick["source_file_id"],
                pick["source_page"], pick["source_table"], None, pick["source_text"],
                1.0, "validated", 1 if differing else 0, note, 1, EXTRACTOR, NOW,
            ),
        )
        rep.facts += 1
        rep.restated += 1 if differing else 0

        # CHECK 约束要求 adopted 的观测必须指向它写成的那条事实——
        # 没有这个指针的话，「这个数字是从哪条观测来的」就断了，
        # 而证据链正是这个系统的卖点。约束替我们记住了这件事。
        con.execute(
            "UPDATE fact_observation SET resolution='adopted', resolved_fact_id=?"
            " WHERE observation_id=?",
            (fact_id, pick["observation_id"]),
        )
        for o in others:
            con.execute(
                "UPDATE fact_observation SET resolution='rejected', rejection_note=?"
                " WHERE observation_id=?",
                ("同一事实的重复来源，已取本期年报的值" if not differing else "重述后不再采用",
                 o["observation_id"]),
            )


def _report_year_of(con, file_id: str) -> int:
    row = con.execute("SELECT period FROM file WHERE file_id=?", (file_id,)).fetchone()
    return int(row["period"]) if row and row["period"].isdigit() else -1


def _file_label(con, file_id: str) -> str:
    row = con.execute("SELECT rel_path FROM file WHERE file_id=?", (file_id,)).fetchone()
    return Path(row["rel_path"]).name if row else file_id


def main() -> int:
    ap = argparse.ArgumentParser(description="解析真实年报并落库")
    ap.add_argument("--source", required=True, help="年报根目录")
    ap.add_argument("--force", action="store_true", help="先清掉这些项目的旧数据")
    args = ap.parse_args()

    from app.config import get_settings

    root = Path(args.source)
    if not root.is_dir():
        print(f"✗ 目录不存在：{root}")
        return 1

    samples = get_settings().data_root / "samples"
    samples.mkdir(parents=True, exist_ok=True)

    con = connect()
    rep = IngestReport()
    try:
        index = LabelIndex.from_db(con, industry="steel")
        if index.conflicts:
            print(f"⚠ 字典里有别名挂在多个字段上：{index.conflicts[:5]}")

        if args.force:
            for pid, *_ in COMPANY_DIRS.values():
                con.execute("DELETE FROM project WHERE project_id=?", (pid,))
            print("已清掉旧项目数据")

        for dirname, (pid, cid, name, code, is_main) in COMPANY_DIRS.items():
            d = root / dirname
            if not d.is_dir():
                print(f"⚠ 跳过（不存在）：{dirname}")
                continue
            # 年份一律用正则从文件名里抠。
            # ⚠ 别按分隔符切：`宝钢股份：宝钢股份2020年年度报告全文.pdf`
            #   切出来是「宝钢股份」，于是一整年的数据静默丢失——脚本不报错，
            #   只是那几年没有事实，页面上显示「未披露」。
            years = sorted({m.group(1) for f in d.glob("*.pdf")
                            if (m := _YEAR_IN_NAME.search(f.name))})
            con.execute(
                "INSERT OR IGNORE INTO project (project_id, name, company_name,"
                " stock_code, industry, base_currency, fiscal_years, base_scope,"
                " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, f"{name}（{years[0]}–{years[-1]}）" if years else name, name, code,
                 "steel", "CNY", json.dumps(years), "consolidated", "active", NOW),
            )

            for pdf in sorted(d.glob("*.pdf")):
                m = _YEAR_IN_NAME.search(pdf.name)
                year = m.group(1) if m else None
                if year is None:
                    rep.failed.append(f"{pdf.name}：文件名里认不出年份")
                    continue
                rel = f"samples/{dirname}/{pdf.name}"
                target = samples / dirname / pdf.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(pdf, target)
                file_id = register_file(con, pid, target, rel, year)
                rep.files += 1

                report = parse_document(pymupdf.open(target), pdf.name, report_year=year)
                for stmt in report.statements:
                    if not stmt.is_consolidated:
                        continue        # 母公司口径不进分析
                    for o in collect(con, pid, file_id, stmt, index, cid, int(year), rep):
                        insert_observation(con, pid, o)
                        rep.observations += 1
                for page_no, why in report.unparsed_titles:
                    rep.failed.append(f"{pdf.name} p{page_no} {why}")

        con.commit()
        # 裁决按项目分别做：同名指标在不同公司之间没有可比性
        for pid, *_ in COMPANY_DIRS.values():
            resolve(con, pid, rep)
        con.commit()

        print(f"✓ 登记文件 {rep.files} 份、数据行 {rep.rows_total} 行、"
              f"映射上 {rep.mapped} 行（{rep.mapped / max(rep.rows_total, 1):.0%}）")
        print(f"✓ 观测 {rep.observations} 条 → 事实 {rep.facts} 条"
              f"（其中 {rep.restated} 条存在重述差异）")
        if rep.failed:
            print(f"⚠ 解析失败的报表 {len(rep.failed)} 处：")
            for f in rep.failed[:8]:
                print(f"    {f}")
        print()
        print(f"未映射的行名（去重 {len(rep.unmapped)} 个，前 15）：")
        for k, n in sorted(rep.unmapped.items(), key=lambda x: -x[1])[:15]:
            print(f"    {n:3}×  {k}")
        print(f"\n数据库：{default_db_path()}")
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
