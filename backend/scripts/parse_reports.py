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

# _console 与本文件同目录。**这一行不能省**：
# 直接 `python scripts/x.py` 时 Python 会自动把脚本目录放进 sys.path，
# 但测试用 `spec_from_file_location` 按路径加载脚本时**不会**——
# 少了它，`import _console` 只在跑测试时炸，看起来像测试坏了。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402  (与本文件同目录)

_console.setup()

import pymupdf  # noqa: E402

from app.db.session import connect, default_db_path  # noqa: E402
from app.parsing import parse_document  # noqa: E402
from app.parsing.extra import EXTRA_SECTIONS, read_section  # noqa: E402
from app.parsing.production_sales import read_production_sales  # noqa: E402
from app.parsing.physical_sales import read_physical_sales  # noqa: E402
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
    #: 同一个数字出现在报表的哪个位置。**位置不同可信度不同**：
    #: 三张主表 > 主要指标表 > 附注 > 正文叙述。
    location: str = "main_statement"

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
    """登记一份 PDF。已登记过就返回原来的 file_id。

    ⚠ **file_id 由内容算出来，不用 uuid4。**

    它原来是 `f-{uuid4 前 12 位}`，后果是一条很长的静默链：

        file_id 随机  →  section_id = md-{file_id}-{序号} 变
                      →  claim_id = sha1(项目|章节|句序|原文) 变
                      →  **会计填好的 P 表一个编号都对不上**

    也就是说，任何人跑一次 `init_db.py --force` 再重新解析，
    会计的活儿就全废了——**而文档里写的却是「确定性 id，防重跑重复靠的是它」**。
    那句话在同一次解析内是对的，跨一次解析就是错的，两件事长得一样。

    2026-10-06 实测撞上：重建一次，交回来的 121 行**一行都对不上**。
    幸而原文可用来重新编号（见 `scripts/export_input_templates.py --rekey`），
    才没让会计白填。

    改成按**相对路径 + 文件 sha256** 算，同样的年报重新解析还是同一个 id。

    > 注意这**没有**让 `claim_id` 完全稳定：解析规则一改（分段、句切分），
    > 章节序号和句序仍会平移。所以 `--rekey` 仍然需要留着——
    > 两者是互补的，一个防「重新解析」，一个救「改了规则」。
    """
    row = con.execute(
        "SELECT file_id FROM file WHERE project_id=? AND period=? AND role='annual_report'",
        (project_id, year),
    ).fetchone()
    if row:
        return row["file_id"]
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    file_id = "f-" + hashlib.sha256(
        f"{rel}\x00{digest}".encode("utf-8")
    ).hexdigest()[:12]
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


def collect_extra(
    con, table, index: LabelIndex, value_types: dict[str, str],
    company_id: str, file_id: str, report_year: int, rep: IngestReport,
) -> list[Observation]:
    """一张**附加表**（主要会计数据 / 非经常性损益 / 现金流量表补充资料）→ 若干观测。

    与 `collect()` 的区别只有一处：期间不是「列」而是「行里的字典键」——
    附加表一节里可能有好几个表头段，各段的年份集合不一定一样
    （见 `extra.py` 的说明），所以 `ExtraRow.values` 是 `{期间: 值}`。
    """
    out: list[Observation] = []
    for row in table.rows:
        rep.rows_total += 1
        # ⚠ 两种来源的行名，**必须分开认**：
        #   · 行名是年报上的科目名 → 走字段字典（原来的路径）
        #   · 行名**已经是 metric_key** → `named_columns` 模式按（行, 列）取出来的
        #     格子，它本来就不该有别名：专项储备表的行名一律叫「安全生产费」，
        #     四个数是四件事，靠行名映射会把期初余额当成计提数收下。
        #     这类规则写在 `extra.py` 的 `CellRule` 里，是显式配置，不是猜。
        metric_key = index.match(row.label).metric_key
        if metric_key is None and row.label in value_types:
            metric_key = row.label
        if not metric_key:
            from app.parsing.mapping import normalize_label

            key = normalize_label(row.label)
            if key:
                rep.unmapped[key] = rep.unmapped.get(key, 0) + 1
            continue
        rep.mapped += 1
        # 期间数还是时点数，**由字段的性质决定，不由表的种类决定**。
        # 「主要会计数据」上半张是期间数（营业收入）、下半张是时点数（总资产），
        # 同一张表里两种都有；按表的种类一刀切会把总资产标成期间数。
        kind = "instant" if value_types.get(metric_key) == "stock" else "current"
        # `ExtraRow.values` 与 `ExtraTable.periods` 一一对应，空位是 None。
        for period, value in zip(table.periods, row.values):
            if value is None:
                continue
            # 单位**逐行**取：`named_columns` 模式的同一张表里可以混着
            # 「元」和「%」两种列（产能表就是），拿整张表的因子一乘就错了。
            row_unit = row.unit or table.unit
            row_factor = (
                row.unit_factor if row.unit_factor is not None
                else table.unit_factor
            )
            millions = (value * row_factor).quantize(Decimal("0.000001"))
            out.append(Observation(
                company_id=company_id,
                metric_key=metric_key,
                period=period,
                period_kind=kind,
                scope=table.scope,
                value_raw=str(value),
                raw_unit=row_unit,
                value_millions=millions,
                file_id=file_id,
                source_page=row.page_no,
                source_table=table.title,
                source_text=row.source_text,
                report_year=report_year,
                label=row.label,
                location=table.location,
            ))
    return out


#: 产销量表上的单位 → 吨的换算因子。**逐表读，绝不默认**——
#: 宝钢写「万吨」、首钢写「吨」，差 10000 倍，而**页面上看起来一样正常**。
_TON_FACTORS = {
    "吨": Decimal("1"), "千吨": Decimal("1000"),
    "万吨": Decimal("10000"), "百万吨": Decimal("1000000"),
}

#: 产销量表「合计」行的哪一列 → 哪个字段。
#:
#: ⚠ **只收宝钢这一种版式。** 会计 2026-10-06 答复原话：「华菱『钢铁行业』、
#: 首钢『冶金』只有行业聚合口径，年报没有明确说明是钢材还是粗钢，
#: **不自动映射**到 steel_sales 或 steel_production」。而宝钢那张表是按产品
#: 拆分的、合计行口径清楚，答复允许「记录为『披露范围内钢铁产品产销量』」——
#: 范围说明写在字典里这两个字段的 `scope_note` 上。
_PRODUCTION_FIELDS = (("output", "steel_output"), ("sales", "steel_sales_volume"))


def collect_production_sales(
    table, company_id: str, file_id: str, report_year: int, rep: IngestReport,
) -> list[Observation]:
    """产销量表的合计行 → 两条观测（产量、销量）。"""
    factor = _TON_FACTORS.get(table.unit)
    if factor is None:
        # 认不出单位就**整表不收**。默认成「吨」的话数字差一万倍，
        # 而吨钢毛利、产能利用率这些下游全都建立在它上面。
        rep.failed.append(f"产销量表单位 {table.unit!r} 认不出，整表不收")
        return []
    out: list[Observation] = []
    for row in table.rows:
        rep.rows_total += len(_PRODUCTION_FIELDS)
        rep.mapped += len(_PRODUCTION_FIELDS)
        for attr, metric_key in _PRODUCTION_FIELDS:
            value = getattr(row, attr)
            out.append(Observation(
                company_id=company_id, metric_key=metric_key,
                period=str(report_year), period_kind="current",
                scope="consolidated", value_raw=str(value), raw_unit=table.unit,
                value_millions=(value * factor).quantize(Decimal("0.000001")),
                file_id=file_id, source_page=row.page_no,
                source_table="产销量情况分析表", source_text=row.source_text,
                report_year=report_year, label="合计",
                # 位置就是可信度：这张表在 MD&A 正文里，不是审计过的三张主表
                location="mdna_text",
            ))
    return out


def collect_physical_sales(
    row, company_id: str, file_id: str, report_year: int, rep: IngestReport,
) -> list[Observation]:
    """行业聚合销量的 `销售量` 行 → 一条观测，落 `industry_sales_volume`。

    ⚠ **不查字段字典。** `销售量` 这个别名在字典里同时挂在三个指标上
    （钢材销量 / 商品煤销量 / 行业聚合销量），`LabelIndex` 冲突时留先来的那一个
    ——留的是 `steel_sales_volume`，**正是会计点名不许的那个**。
    所以口径写成显式代码：这张表的 `销售量` 行只落 `industry_sales_volume`。
    理由与会计答复原文见 `app/parsing/physical_sales.py` 的模块 docstring。
    """
    rep.rows_total += 1
    rep.mapped += 1
    label = f"销售量（{row.industry}）" if row.industry else "销售量"
    return [Observation(
        company_id=company_id, metric_key="industry_sales_volume",
        period=row.period, period_kind="current",
        scope="consolidated", value_raw=str(row.raw_value), raw_unit=row.raw_unit,
        value_millions=row.tons,
        file_id=file_id, source_page=row.page_no,
        source_table="公司实物销售收入是否大于劳务收入",
        source_text=row.source_text,
        report_year=report_year, label=label,
        location="mdna_text",
    )]


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
         o.source_table, o.location, o.source_text, 1.0, EXTRACTOR, NOW, "pending"),
    )
    return oid


#: `unit_kind` → 落进 `financial_fact.unit` 的展示单位。
#: 金额一律折成百万元；其余按字段性质给，**不再一律「百万元」**。
_UNIT_BY_KIND = {
    "currency": "百万元", "percent": "%", "ton": "吨", "days": "天",
    "shares": "股", "quantity": "数量", "text": "文本",
}


def resolve(con, project_id: str, rep: IngestReport,
            unit_kinds: dict[str, str] | None = None) -> None:
    """观测 → 事实。

    ⚠ **「本期年报优先」**：2023 年的数，2023 年报说了算。
    后来的年报对上一年重述过，那个重述值不覆盖原始披露值，另存一行
    （`restated=1`）并说明差异。这是 CLAUDE.md 里「重述值与原值双行并存，
    永不 UPDATE 覆盖」那条硬规则——它保护的是「当时到底披露了什么」这个事实。
    """
    unit_kinds = unit_kinds or {}
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
        existing = con.execute(
            "SELECT fact_id, value_millions, restated FROM financial_fact"
            " WHERE project_id=? AND company_id=? AND metric_key=? AND period=?"
            "   AND period_kind=? AND scope=?",
            (project_id, company_id, metric_key, period, pick["period_kind"], scope),
        ).fetchall()

        if existing:
            # ⚠ **这个 (指标, 期间, 口径) 库里已经有了 —— 绝不覆盖。**
            #
            # 这条分支是补出来的，因为原来的 INSERT 是**裸的**：同一个库上跑
            # 第二遍 `parse_reports.py`，`resolve` 会撞
            # `UNIQUE(project, company, metric, period, period_kind, scope, restated)`
            # 直接抛 IntegrityError，**整批回滚**。
            #
            # 「重述值与原值双行并存、永不 UPDATE 覆盖」本来就是硬规则，
            # 过去没暴露只是因为没人重跑过：三张主表建完库就齐了。
            # 加上附加表之后，「补跑一次解析」成了常规操作（附加表的定位规则
            # 一改就得重跑），所以这条必须补。
            #
            # 值一致 → 直接把观测挂到已有那条事实上，不新增；
            # 值不同 → 另存一条 `restated=1`，不碰原值。
            base = next((e for e in existing if e["restated"] == 0), None)
            if base is not None and base["value_millions"] == pick["value_millions"]:
                # 值一致 → 把观测挂到已有那条事实上，**不新增一行**。
                fact_id = base["fact_id"]
            elif base is None:
                fact_id = existing[0]["fact_id"]   # 只有重述行、没有原作，到不了
            else:
                prior_note = (
                    f"已有事实记 {base['value_millions']}（{base['fact_id']}）；"
                    f"本次解析得到 {pick['value_millions']}"
                )
                note = f"{note}；{prior_note}" if note else prior_note
                dup = next(
                    (e for e in existing if e["restated"] == 1
                     and e["value_millions"] == pick["value_millions"]),
                    None,
                )
                if dup is not None:
                    fact_id = dup["fact_id"]
                else:
                    con.execute(
                        "INSERT INTO financial_fact (fact_id, project_id, company_id,"
                        " is_primary, metric_key, value_millions, value_raw, raw_unit,"
                        " unit_factor, unit, period, period_kind, scope, source_file,"
                        " source_file_id, source_page, source_table, source_row_label,"
                        " source_text, confidence, status, restated, restatement_note,"
                        " comparable, extractor, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            fact_id, project_id, company_id,
                            1 if company_id.endswith("600019") else 0,
                            metric_key, pick["value_millions"], pick["value_raw"],
                            pick["raw_unit"],
                            str(Decimal(pick["value_millions"]) / Decimal(pick["value_raw"])
                                if Decimal(pick["value_raw"]) else Decimal("1")),
                            _UNIT_BY_KIND.get(unit_kinds.get(metric_key, "currency"), "百万元"),
                period, pick["period_kind"], scope,
                            _file_label(con, pick["source_file_id"]),
                            pick["source_file_id"], pick["source_page"],
                            pick["source_table"], None, pick["source_text"],
                            1.0, "validated", 1, note, 1, EXTRACTOR, NOW,
                        ),
                    )
                    rep.facts += 1
                    rep.restated += 1
        else:
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
                    _UNIT_BY_KIND.get(unit_kinds.get(metric_key, "currency"), "百万元"),
                period, pick["period_kind"], scope,
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
        #
        # ⚠ **但每个键至多只能有一条 adopted 观测**（`ux_obs_one_adopted`）。
        #   重跑一次解析会给同一个键再造一批观测，如果无脑再 adopt 一条，
        #   就撞这个部分唯一索引——报的错是
        #   `UNIQUE constraint failed: fact_observation...`，
        #   而真正的原因（「这一笔早就解析过了」）在错误信息里一个字都没有。
        #   所以先看有没有，有就把本次的记为重复来源。
        already = con.execute(
            "SELECT observation_id, resolved_fact_id FROM fact_observation"
            " WHERE project_id=? AND company_id=? AND metric_key=? AND period=?"
            "   AND period_kind=? AND scope=? AND resolution='adopted'",
            (project_id, company_id, metric_key, period, pick["period_kind"], scope),
        ).fetchone()
        if already is not None:
            con.execute(
                "UPDATE fact_observation SET resolution='rejected', rejection_note=?"
                " WHERE observation_id=?",
                (f"重复来源：该键已有被采纳的观测（{already['observation_id']}），"
                 f"本轮解析得到的值另见 {fact_id}",
                 pick["observation_id"]),
            )
            for o in others:
                if o["observation_id"] == already["observation_id"]:
                    continue
                con.execute(
                    "UPDATE fact_observation SET resolution='rejected',"
                    " rejection_note=? WHERE observation_id=?",
                    ("重复来源：该键已有被采纳的观测", o["observation_id"]),
                )
            continue

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
        # 附加表的期间数/时点数由**字段本身**的性质决定（见 collect_extra）。
        value_types = {
            r["metric_key"]: r["value_type"]
            for r in con.execute("SELECT metric_key, value_type FROM metric_definition")
        }
        # 落库的展示单位也按字段性质定，**不能一律写「百万元」**。
        # 原来 `resolve()` 把 unit 硬编码成「百万元」，那是因为在此之前
        # 库里只有金额型的字段——加上「年报披露产能利用率」这类百分比字段之后，
        # 97% 会带着单位「百万元」显示在证据面板上，而**没有任何地方会报错**。
        unit_kinds = {
            r["metric_key"]: r["unit_kind"]
            for r in con.execute("SELECT metric_key, unit_kind FROM metric_definition")
        }

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
            # ⚠ **已有的项目要把派生字段更新掉，不能 OR IGNORE 就完事。**
            #
            # `fiscal_years` 不是「当初建项目时是哪几年」，而是
            # 「**这个项目现在有哪些年**」——补年报之后它就是新的了。
            # 原来写的是 `INSERT OR IGNORE`，已存在就整行跳过，
            # 于是补进来 6 份年报、库里事实也进了，
            # 而项目还写着老年份。后果是**静默的**：
            #
            #   · `v_fact_grid_company` 按 `json_each(fiscal_years)` 展开年度列，
            #     补的那 6 年在**总表里一列都不显示**；
            #   · `_load_risk_items` 也按它展开「年度 × 四项」，
            #     新年度不会被要求核验，R 于是**看起来是完整的**。
            #
            # 两处都算得出结果，看起来都很正常，只是说的是旧的那几年。
            con.execute(
                "INSERT INTO project (project_id, name, company_name,"
                " stock_code, industry, base_currency, fiscal_years, base_scope,"
                " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project_id) DO UPDATE SET"
                " name=excluded.name, fiscal_years=excluded.fiscal_years",
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

                doc = pymupdf.open(target)
                report = parse_document(doc, pdf.name, report_year=year)
                for stmt in report.statements:
                    if not stmt.is_consolidated:
                        continue        # 母公司口径不进分析
                    for o in collect(con, pid, file_id, stmt, index, cid, int(year), rep):
                        insert_observation(con, pid, o)
                        rep.observations += 1
                for page_no, why in report.unparsed_titles:
                    rep.failed.append(f"{pdf.name} p{page_no} {why}")

                # ---- 三张主表之外的规整表格 --------------------------------
                # 一张张来，任何一张抽不出来都**只记进 rep.failed**，不影响其余。
                # 抽不出来必须出声：静默跳过的话，那些指标在页面上显示
                # 「未在年报中定位到」，而真实原因是**那张表压根没人去读**，
                # 两件事看起来一模一样。
                # ---- 产销量情况分析表（只有宝钢这一种版式）-----------------
                ps = read_production_sales(doc, year)
                if ps is None:
                    rep.failed.append(
                        f"{pdf.name}：产销量情况分析表 没抽出来（这类公司本来就没有）"
                    )
                else:
                    for o in collect_production_sales(ps, cid, file_id, int(year), rep):
                        insert_observation(con, pid, o)
                        rep.observations += 1

                # ---- 行业聚合销量（华菱 / 首钢那张表）---------------------
                # 与上面互斥：宝钢走产销量表、另两家走实物销售表，一家只会命中一张。
                # 两条都落不着的年份**不出声**——这张表不是每家每年都有，
                # 记进 rep.failed 会把这个数字刷成几十条噪音，把真失败淹掉。
                phys = read_physical_sales(doc, year)
                if phys is not None:
                    for o in collect_physical_sales(phys, cid, file_id, int(year), rep):
                        insert_observation(con, pid, o)
                        rep.observations += 1

                for spec in EXTRA_SECTIONS:
                    table = read_section(doc, spec, year)
                    if table is None:
                        rep.failed.append(
                            f"{pdf.name}：{spec.key} 这一节没抽出来（标题或表头认不出）"
                        )
                        continue
                    for o in collect_extra(
                        con, table, index, value_types, cid, file_id, int(year), rep
                    ):
                        insert_observation(con, pid, o)
                        rep.observations += 1

        con.commit()
        # 裁决按项目分别做：同名指标在不同公司之间没有可比性
        for pid, *_ in COMPANY_DIRS.values():
            resolve(con, pid, rep, unit_kinds)
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
