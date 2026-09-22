"""灌一份**演示数据**：宝钢股份 2015–2024 的十年财务事实。

用法：
    python scripts/seed_demo.py            # 已有演示项目则跳过
    python scripts/seed_demo.py --force    # 删掉重建

**为什么需要它。** 业务表全空的时候，前端 8 个页面有 7 个点开是空白——
丙 把页面写完了也验证不了，因为没有任何数据流过去。这份数据的作用是让
「界面能不能正确渲染」和「PDF 解析得准不准」这两件事**解耦**：解析还没做，
页面照样能开发和验收。

## ⚠ 这些数字是合成的，不是宝钢的真实财报

数值的量级和周期形状是照着钢铁行业做的（2015 谷底 → 2018 高点 →
2021 大宗暴涨 → 2022 起回落），但**每一个数字都是程序生成的，不是任何一份
年报的原文**。所以它在三个地方被标成演示数据，而不是靠使用者记得：

    project.name              前缀「【演示数据】」，页面上第一眼就能看到
    financial_fact.extractor  'demo:seed_v1'，可按它整批查出 / 删掉
    financial_fact.source_text 前缀「【演示数据】」，**证据面板里也逃不掉**

第三条最要紧。这个系统的卖点是「点任意结论都能回到年报原文」，
如果演示数据的 source_text 长得和真原文一样，那它在证据面板里就是
**一句看起来很真的假话**——和 CLAUDE.md 里那条「标为 annual_report 就必须
带出处」是同一个道理：证据长得像证据，但它不是，这件事必须写在脸上。

## 数据必须自洽，而且**从库里就能勾稽**

写库之前先断言，写完之后**只从库里读**再断言一次（`verify_from_db`）。
理由：乙 的勾稽检查跑的是 SQL，不是这个脚本里的内存变量。如果恒等式只在
生成时成立、落库时被字段错位破坏，内存里的断言一个字都不会响——
而界面上会显示一份「资产 ≠ 负债 + 权益」的报表。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _console  # noqa: E402  (与本文件同目录)

_console.setup()

from app.db.session import connect  # noqa: E402

PROJECT_ID = "p-demo600019"
COMPANY_ID = "c-600019"
COMPANY_NAME = "宝钢股份"
STOCK_CODE = "600019.SH"
INDUSTRY = "steel"
YEARS = [str(y) for y in range(2015, 2025)]

EXTRACTOR = "demo:seed_v1"
#: 三处标记之一。证据面板直接显示 source_text，所以这句必须写在最前面。
DEMO_TAG = "【演示数据】"
UNIT = "百万元"

#: 期间费用率。**刻意固定**：这样营业利润完全由毛利率推出来，
#: 利润表从「毛利」到「营业利润」每一行都能从库里加出来对上，
#: 乙 的勾稽检查才有真东西可查。
RATIOS = {
    "taxes_and_surcharges": "0.006",
    "selling_expense": "0.004",
    "admin_expense": "0.012",
    "rd_expense": "0.014",
    "finance_expense": "0.004",
}
EXPENSE_RATIO = sum(Decimal(v) for v in RATIOS.values())

#: 十年周期形状。单位：亿元。
#:
#: 这些数是**照着钢铁周期编的**，不是宝钢的真实财报：2015 全行业亏损、
#: 2016 供给侧改革、2018 高点、2021 大宗暴涨、2022 起需求回落。
#: 周期正常化引擎（8 年窗口 + 高低盈利阶段覆盖校验）要能在这份数据上跑出
#: 一个像样的中枢，所以形状必须真的有起伏——一条平线是测不出东西的。
PLAN: dict[str, dict[str, str]] = {
    #      营业收入  毛利率  资产总计  有息负债  资本开支  经营现金流
    "2015": dict(rev="1640", gm="0.045", ta="2380", debt="560", capex="96",  cfo="188"),
    "2016": dict(rev="1850", gm="0.081", ta="2520", debt="540", capex="88",  cfo="264"),
    "2017": dict(rev="2600", gm="0.128", ta="2870", debt="520", capex="112", cfo="382"),
    "2018": dict(rev="3050", gm="0.139", ta="3080", debt="500", capex="146", cfo="428"),
    "2019": dict(rev="2920", gm="0.104", ta="3160", debt="495", capex="168", cfo="336"),
    "2020": dict(rev="2840", gm="0.098", ta="3320", debt="540", capex="152", cfo="298"),
    "2021": dict(rev="3650", gm="0.152", ta="3720", debt="580", capex="186", cfo="512"),
    "2022": dict(rev="3690", gm="0.083", ta="3910", debt="660", capex="214", cfo="304"),
    "2023": dict(rev="3440", gm="0.079", ta="4020", debt="720", capex="228", cfo="276"),
    "2024": dict(rev="3220", gm="0.071", ta="4080", debt="780", capex="236", cfo="242"),
}


def _d(x: str | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(x)


def _r(x: Decimal) -> Decimal:
    """四舍五入到整数（百万元）。小数会让恒等式差一两块，看起来像 bug。"""
    return x.quantize(_d("1"))


def build_years() -> list[dict]:
    """把周期形状展开成一份**内部自洽**的十年数据。

    只定「收入 / 毛利率 / 资产 / 有息负债 / 资本开支 / 经营现金流」六个自由量，
    其余全部由会计关系推出来。手填 350 个数字必然有对不上的，而那种错
    **不会报错**，只会在某一张报表上显示一个不平的数。
    """
    out: list[dict] = []
    prev_cash = _d("520")
    prev_equity = _d("1180")

    for year in YEARS:
        p = PLAN[year]
        rev, ta, debt = _d(p["rev"]), _d(p["ta"]), _d(p["debt"])

        # —— 利润表：从毛利一路推到净利润
        gross = _r(rev * _d(p["gm"]))
        cost = rev - gross
        exp = {k: _r(rev * _d(v)) for k, v in RATIOS.items()}
        op_profit = gross - sum(exp.values())
        ebit = op_profit + exp["finance_expense"]
        # 简化：营业外收支净额为零，利润总额 = 营业利润
        pbt = op_profit
        tax = _r(max(pbt, _d("0")) * _d("0.25"))
        net = pbt - tax
        net_parent = _r(net * _d("0.94"))
        minority_pl = net - net_parent

        # —— 资产负债表：权益从上年滚上来，负债倒挤，恒等式天然成立
        dividends = _r(net_parent * _d("0.35"))
        equity = prev_equity + net - dividends
        liab = ta - equity
        minority = _r(equity * _d("0.06"))
        equity_parent = equity - minority

        # —— 现金流量表：现金必须滚得上
        cfo, capex = _d(p["cfo"]), _d(p["capex"])
        cfi = -capex - _r(rev * _d("0.004"))
        cff = -(cfo + cfi) - _r(rev * _d("0.010"))
        net_cash = cfo + cfi + cff
        cash_end = prev_cash + net_cash
        # 现金要装得进资产里，否则资产结构不成立
        assert cash_end < ta * _d("0.35"), f"{year} 现金占资产比重过高，结构不合理"

        dep = _r(rev * _d("0.052"))

        out.append({
            "year": year,
            "revenue": rev, "total_revenue": rev, "operating_cost": cost,
            "gross_profit": gross, **exp,
            "operating_profit": op_profit, "ebit": ebit,
            "profit_before_tax": pbt, "income_tax": tax,
            "net_income": net, "net_income_parent": net_parent,
            "minority_interest_pl": minority_pl,
            "depreciation_amortization": dep, "ebitda": ebit + dep,
            "total_assets": ta, "total_liabilities": liab, "total_equity": equity,
            "equity_parent": equity_parent, "minority_interest": minority,
            "interest_bearing_debt": debt,
            "cfo": cfo, "cfi": cfi, "cff": cff, "capex": capex,
            "cash_begin": prev_cash, "cash_end": cash_end, "cash_net_increase": net_cash,
            # 无恒等式约束，按资产结构给固定比例，保证年份之间可横向比较
            "cash_and_equivalents": cash_end,
            "inventory": _r(rev * _d("0.115")),
            "notes_and_ar": _r(rev * _d("0.042")),
            "ppe": _r(ta * _d("0.455")),
            "cip": _r(ta * _d("0.048")),
            "short_term_borrowing": _r(debt * _d("0.62")),
            "long_term_borrowing": debt - _r(debt * _d("0.62")),
        })
        prev_cash, prev_equity = cash_end, equity

    return out


def assert_consistent(years: list[dict]) -> None:
    """写库之前的自检。**这些恒等式不成立就别写。**"""
    for y in years:
        tag = y["year"]
        assert y["total_assets"] == y["total_liabilities"] + y["total_equity"], f"{tag} 资产 ≠ 负债 + 权益"
        assert y["gross_profit"] == y["revenue"] - y["operating_cost"], f"{tag} 毛利 ≠ 收入 − 成本"
        assert y["operating_profit"] == y["gross_profit"] - sum(
            y[k] for k in RATIOS
        ), f"{tag} 营业利润 ≠ 毛利 − 期间费用"
        assert y["net_income"] == y["profit_before_tax"] - y["income_tax"], f"{tag} 净利润对不上"
        assert y["cash_net_increase"] == y["cfo"] + y["cfi"] + y["cff"], f"{tag} 现金净增加对不上"
        assert y["cash_end"] == y["cash_begin"] + y["cash_net_increase"], f"{tag} 现金滚动对不上"
        assert y["cash_end"] > 0, f"{tag} 现金为负，资产负债表不成立"
    for a, b in zip(years, years[1:]):
        assert b["cash_begin"] == a["cash_end"], f"{a['year']}→{b['year']} 现金不连续"


def verify_from_db(con) -> None:
    """**从库里读回来**再验一遍。内存里对、落库时错位，是这一类脚本最典型的翻车方式。"""
    for year in YEARS:
        rows = dict(con.execute(
            "SELECT metric_key, value_millions FROM financial_fact"
            " WHERE project_id=? AND period=? AND company_id=?",
            (PROJECT_ID, year, COMPANY_ID),
        ).fetchall())
        assert rows, f"{year} 一条事实都没落库"
        get = lambda k: Decimal(rows[k])  # noqa: E731
        assert get("total_assets") == get("total_liabilities") + get("total_equity"), (
            f"{year} 落库后资产 ≠ 负债 + 权益（字段错位？）"
        )
        assert get("gross_profit") == get("revenue") - get("operating_cost"), f"{year} 落库后毛利对不上"
        assert get("operating_profit") == get("gross_profit") - sum(
            get(k) for k in RATIOS
        ), f"{year} 落库后营业利润对不上"
        assert get("cash_end") == get("cash_begin") + get("cash_net_increase"), f"{year} 落库后现金对不上"

    # 演示标记必须真的写进去了——漏了的话证据面板会把它显示成年报原文
    unmarked = con.execute(
        "SELECT COUNT(*) FROM financial_fact WHERE project_id=? AND"
        " (extractor <> ? OR source_text NOT LIKE ?)",
        (PROJECT_ID, EXTRACTOR, f"{DEMO_TAG}%"),
    ).fetchone()[0]
    assert unmarked == 0, f"有 {unmarked} 条事实没打演示标记"


#: (指标键, 报表, 页号, 原始行名, period_kind, mapped_from)
#:
#: 页号是编的，但**偏移量随年份递增**：年报越厚，主表位置越靠后。
#: 恒定页号会让「按页码定位」这类逻辑在演示时表现不出真实行为。
FACT_SPECS: list[tuple[str, str, int, str, str, str | None]] = [
    # revenue 只披露「营业总收入」——正是签字文档 A-2 那条映射规则的场景：
    # 值其实抽自 total_revenue 那一行，必须记下 mapped_from，
    # 否则按 (metric_key, period) 聚合时会把它和真正的 revenue 再加一遍。
    ("revenue",              "income",   78, "营业总收入",  "current", "total_revenue"),
    ("total_revenue",        "income",   78, "营业总收入",  "current", None),
    ("operating_cost",       "income",   78, "其中：营业成本", "current", None),
    ("gross_profit",         "income",   78, "毛利",        "current", None),
    ("taxes_and_surcharges", "income",   79, "税金及附加",   "current", None),
    ("selling_expense",      "income",   79, "销售费用",     "current", None),
    ("admin_expense",        "income",   79, "管理费用",     "current", None),
    ("rd_expense",           "income",   79, "研发费用",     "current", None),
    ("finance_expense",      "income",   79, "财务费用",     "current", None),
    ("operating_profit",     "income",   80, "营业利润",     "current", None),
    ("profit_before_tax",    "income",   80, "利润总额",     "current", None),
    ("income_tax",           "income",   80, "所得税费用",   "current", None),
    ("net_income",           "income",   81, "净利润",       "current", None),
    ("net_income_parent",    "income",   81, "归属于母公司股东的净利润", "current", None),
    ("minority_interest_pl", "income",   81, "少数股东损益",  "current", None),
    ("ebit",                 "income",   81, "息税前利润",   "current", None),
    ("ebitda",               "income",   81, "息税折旧摊销前利润", "current", None),
    ("depreciation_amortization", "cashflow", 88, "固定资产折旧、油气资产折耗、生产性生物资产折旧", "current", None),
    ("cfo",                  "cashflow", 88, "经营活动产生的现金流量净额", "current", None),
    ("cfi",                  "cashflow", 89, "投资活动产生的现金流量净额", "current", None),
    ("cff",                  "cashflow", 89, "筹资活动产生的现金流量净额", "current", None),
    ("capex",                "cashflow", 89, "购建固定资产、无形资产和其他长期资产支付的现金", "current", None),
    ("cash_begin",           "cashflow", 90, "期初现金及现金等价物余额", "opening", None),
    ("cash_net_increase",    "cashflow", 90, "现金及现金等价物净增加额", "current", None),
    ("cash_end",             "cashflow", 90, "期末现金及现金等价物余额", "instant", None),
    ("total_assets",         "balance",  62, "资产总计",     "instant", None),
    ("total_liabilities",    "balance",  62, "负债合计",     "instant", None),
    ("total_equity",         "balance",  62, "所有者权益合计", "instant", None),
    ("equity_parent",        "balance",  62, "归属于母公司所有者权益合计", "instant", None),
    ("minority_interest",    "balance",  62, "少数股东权益",  "instant", None),
    ("cash_and_equivalents", "balance",  60, "货币资金",     "instant", None),
    ("inventory",            "balance",  60, "存货",        "instant", None),
    ("notes_and_ar",         "balance",  60, "应收票据及应收账款", "instant", None),
    ("ppe",                  "balance",  61, "固定资产",     "instant", None),
    ("cip",                  "balance",  61, "在建工程",     "instant", None),
    ("interest_bearing_debt", "balance", 63, "有息负债合计",  "instant", None),
    ("short_term_borrowing", "balance",  63, "短期借款",     "instant", None),
    ("long_term_borrowing",  "balance",  63, "长期借款",     "instant", None),
]

TABLE_NAME = {
    "balance": "合并资产负债表",
    "income": "合并利润表",
    "cashflow": "合并现金流量表",
}

INSERT_SQL = (
    "INSERT INTO financial_fact (fact_id, project_id, company_id, is_primary,"
    " metric_key, mapped_from, value_millions, value_raw, raw_unit, unit_factor,"
    " unit, period, period_kind, period_start, period_end, scope, source_file,"
    " source_file_id, source_page, source_printed_page, source_table,"
    " source_row_label, source_text, bbox, confidence, status, restated,"
    " restatement_note, comparable, incomparable_reason, extractor, created_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def fact_rows(years: list[dict]) -> list[tuple]:
    rows = []
    for idx, y in enumerate(years):
        year, shift = y["year"], idx // 4
        for key, statement, page, label, kind, mapped_from in FACT_SPECS:
            value = y[key]
            rows.append((
                f"f-demo-{year}-{key}", PROJECT_ID, COMPANY_ID, 1,
                key, mapped_from,
                str(value), str(value * 1_000_000), "元", "0.000001", UNIT,
                year, kind, None, None, "consolidated",
                f"{STOCK_CODE}_{year}_年报.pdf", f"file-demo-{year}",
                page + shift, None, TABLE_NAME[statement], label,
                f"{DEMO_TAG}{year}年{label}为{value}{UNIT}。"
                f"（本句由 scripts/seed_demo.py 合成，非年报原文）",
                None, 1.0, "validated", 0, None, 1, None,
                EXTRACTOR, "2026-09-22T00:00:00+08:00",
            ))
    return rows


def seed(force: bool) -> int:
    con = connect()
    try:
        existing = con.execute(
            "SELECT name FROM project WHERE project_id=?", (PROJECT_ID,)
        ).fetchone()
        if existing and not force:
            print(f"演示项目已存在：{existing['name']}")
            print("要重建请加 --force（会删掉这个项目的全部数据）")
            return 0
        if existing:
            # 外键是 ON DELETE CASCADE，删项目带走 file / financial_fact
            con.execute("DELETE FROM project WHERE project_id=?", (PROJECT_ID,))
            print("已删除旧的演示项目")

        years = build_years()
        assert_consistent(years)
        print(f"✓ 内存自检通过（资产=负债+权益、现金滚动、毛利=收入−成本、营业利润=毛利−费用）")

        con.execute(
            "INSERT INTO project (project_id, name, company_name, stock_code,"
            " industry, base_currency, fiscal_years, base_scope, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                PROJECT_ID, f"{DEMO_TAG}{COMPANY_NAME}（{YEARS[0]}–{YEARS[-1]}）",
                COMPANY_NAME, STOCK_CODE, INDUSTRY, "CNY",
                json.dumps(YEARS, ensure_ascii=False), "consolidated", "active",
                "2026-09-22T00:00:00+08:00",
            ),
        )
        for y in YEARS:
            rel = f"demo/{STOCK_CODE}_{y}_年报.pdf"
            con.execute(
                "INSERT INTO file (file_id, project_id, role, period, rel_path,"
                " sha256, bytes, page_count, is_scanned, parse_status, uploaded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"file-demo-{y}", PROJECT_ID, "annual_report", y, rel,
                    hashlib.sha256(rel.encode()).hexdigest(),
                    4_200_000 + int(y) * 1000, 180 + YEARS.index(y), 0, "parsed",
                    "2026-09-22T00:00:00+08:00",
                ),
            )

        con.executemany(INSERT_SQL, fact_rows(years))
        con.commit()

        verify_from_db(con)  # ← 从库里读回来再验一遍

        n = con.execute(
            "SELECT COUNT(*) FROM financial_fact WHERE project_id=?", (PROJECT_ID,)
        ).fetchone()[0]
        v = con.execute(
            "SELECT COUNT(*) FROM v_fact_verified WHERE project_id=?", (PROJECT_ID,)
        ).fetchone()[0]
        print(f"✓ 从库里复验通过（含演示标记）")
        print(f"  项目 {COMPANY_NAME} {YEARS[0]}–{YEARS[-1]}：文件 {len(YEARS)} 份、"
              f"财务事实 {n} 条（可比已验证 {v} 条）")
        print()
        print("⚠ 这些数字是**合成**的，不是年报原文。三处标记：")
        print(f"  项目名 / extractor='{EXTRACTOR}' / source_text 前缀「{DEMO_TAG}」")
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="灌入演示数据（宝钢十年财务事实）")
    parser.add_argument("--force", action="store_true", help="删除已有演示数据后重建")
    return seed(parser.parse_args().force)


if __name__ == "__main__":
    raise SystemExit(main())
