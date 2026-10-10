"""从年报里把 R（风险披露充分度）的**候选证据**抽出来，供会计逐格判断。

用法：
    python scripts/extract_r_candidates.py                    # 只打印（dry-run）
    python scripts/extract_r_candidates.py --write            # 写库

## ⚠ 候选 ≠ 已核验，而且这一项尤其不能代判

R 的判据是「**同时**说清风险对象、作用路径与可核验依据」。前两样是按项的固定口径
（下面 `ITEM_TEXT`，逐字沿用会计已核的 2022–2024 十二行），
**第三样「可核验依据」只能在原文里找**——而找到了也不等于充分：
首钢的风险章节绝大多数是行业套话加一句「为应对上述风险，公司将……」，
按口径那属于 `insufficient`（「只有出现『风险、压力、可能』等词，不能判充分」）。

所以这个脚本只做两件事：

  1. 把**该格对应的原文片段连同页码**捞出来（`evidence`），
     让会计不必翻 PDF 就能判——`mitigation` 顺手取那一句缓释措施；
  2. 捞不到就**留空**，绝不填一句「年报未披露」之类的推断。

`conclusion` 一律留 `pending`，`reviewer` **留空**。判 sufficient / insufficient
是会计的活，程序代不了。

## 出处顺序（写进 evidence，让人看得见它是从哪来的）

    ① 风险章节（`kind='outlook'` 里的「4、可能面对的风险」）——会计口径 §3.1 指定的出处
    ② 找不到时往「主营业务分析」里找，并在 evidence 里**写明不是风险章节**

⚠ 首钢的风险章节里**没有**「原燃料成本」与「流动性与回款」两个条目
（十份年报都只有政策及行业 / 同业竞争 / 产品价格·营销 / 环保 / 关联交易），
这两项只能靠 ②，而且多数年份捞不到公司特定的依据——那正是它们应当判
`insufficient` 的因素之一，不是脚本的失败。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402

_console.setup()

from app.db.session import connect  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
DB_PATH = BACKEND_DIR / "var" / "finance.db"

#: 四项的固定口径。**逐字沿用会计已核的 2022–2024 那十二行**——
#: 换个写法就等于把同一个风险项描述成两件事，页面上并排看会像两个口径。
ITEM_TEXT: dict[str, tuple[str, str]] = {
    "demand_price": (
        "钢材下游需求与钢材价格波动",
        "需求或价格变化通过销售量/售价传导至营业收入、毛利和估值",
    ),
    "fuel_cost": (
        "铁矿石、煤焦、废钢等原燃料采购价格波动",
        "原燃料采购价格变化传导至单位成本、营业成本和毛利",
    ),
    "environment_capacity": (
        "环保、碳排放约束及钢铁产能/产量政策",
        "环保投入、碳成本或限产政策影响产能利用、成本和可供销售量",
    ),
    "liquidity_collection": (
        "应收账款回款、客户信用及经营现金流",
        "回款速度或信用损失影响应收余额、坏账准备、经营现金流和营运资本",
    ),
}

#: 风险章节的子条目 → `(归到哪一项, 正文要不要再过一遍内容过滤)`。
#:
#: **一个子条目可以归多项**（政策及行业风险同时讲需求、价格与原燃料），
#: 也可以**不归任何项**（同业竞争、关联交易与这四项无关，落不进任何一格）。
#:
#: ⚠ 第二个布尔值是这个表的重点：
#:
#:   · **标题本身就点明了主题**的子条目（产品价格 / 营销 / 环保）→ **False**。
#:     标题已经做过一次归类，正文就是这一项的证据，再过一遍关键词会把它
#:     误杀——实测 2019 的「政策及行业风险」讲的是「供需紧平衡…供销两头」，
#:     一个「需求」「价格」都没有，卡掉它等于报「2019 年报没披露需求风险」。
#:   · **通用桶**「政策及行业风险」→ **True**。它一份年报里可能同时讲政策、
#:     需求、价格、原燃料、竞争，**整段抄过去就是错归**——实测 2017 的
#:     那一段整篇在讲国内外竞争压力，会被当成「需求与钢价」的证据。
SUBITEM_TO_ITEM: dict[str, tuple[tuple[str, bool], ...]] = {
    "政策及行业": (("demand_price", True), ("fuel_cost", True)),
    "产品价格": (("demand_price", False),),
    "市场营销": (("demand_price", False),),
    "营销": (("demand_price", False),),
    "环保": (("environment_capacity", False),),
    "低碳环保": (("environment_capacity", False),),
}

#: 子条目名里带这些词的**不归入**任何一项——它们是另一类风险，
#: 硬归进去会让 R 的分子凭空多出几格。
SUBITEM_EXCLUDE = ("同业竞争", "关联交易", "业务整合", "安全")

#: 每项证据的判据：**必须同时命中两组词**。
#:
#: ⚠ 只要求第一组（对象）远远不够。实测撞到过这些「有对象、说的却是另一件事」：
#:
#:     原燃料检验全自动系统投入使用，实现43个点位原燃料样品送样及检测自动化…
#:        → 含「原燃料」，讲的是**质检自动化**，不是原燃料价格风险
#:     研发投入占营业收入比例  0.43%  … （一整张表）
#:        → 含「营业收入」，是**报表行**，不是风险披露
#:
#: 第二组是「风险/变化」词。两组都命中才算这一项的证据。
ITEM_PATTERN: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "demand_price": (
        ("需求", "钢材价格", "钢价", "销量", "市场", "用钢"),
        ("风险", "下降", "上升", "波动", "压力", "疲弱", "放缓",
         "竞争", "低位", "不足"),
    ),
    "fuel_cost": (
        ("原燃料", "铁矿石", "焦炭", "焦煤", "废钢", "采购"),
        ("价格", "成本", "上涨", "下降", "高位", "波动", "长协",
         "供应", "保障", "风险", "剪刀差"),
    ),
    "environment_capacity": (
        ("环保", "超低排放", "限产", "碳排放", "能耗", "节能减排"),
        ("风险", "压力", "加严", "提高", "约束", "停产", "成本",
         "管控", "改造", "频繁"),
    ),
    "liquidity_collection": (
        ("回款", "应收账款", "现金流量", "坏账", "周转", "信用", "资金"),
        ("风险", "下降", "上升", "占用", "增加", "紧张", "损失",
         "保障", "回收"),
    ),
}

#: 缓释措施那句话的开头。**各家写法不一**——「为应对上述风险」/「为应上述风险」/
#: 「为应对该风险」，漏一种那一句就会**留在 evidence 里**，读起来像
#: 「风险是什么」而实际是「打算怎么办」；`mitigation` 那一列同时空着。
#:
#: ⚠ 中间那几个字用 `.{0,4}?` 兜住，不要枚举——第一次写成
#: `为应[对上该]?[上述]*风险` 时，「为应对**该**风险」匹配不上
#: （可选组吃掉了「对」，剩下的「该」没人接），实测 2017 就这么漏的。
_MITIGATION_RE = re.compile(r"为应.{0,4}?风险[，,]")

#: 证据里开头的子条目标题，如 `(3)产品价格风险`。拼在正文前读起来像标题党，
#: 但它确实是原文的一部分——只把**编号与标题本身**去掉，正文一字不动。
_SUBHEAD_RE = re.compile(r"^[（(]\d+[）)]\s*[^（(。\n]{2,20}?风险\s*")
#: 子条目标题：`(1)政策及行业风险`
_SUBITEM_RE = re.compile(r"^[（(]\d+[）)]\s*([^（(。\n]{2,20}?风险)", re.M)
#: 句子切分
_SPLIT_RE = re.compile(r"(?<=[。；])")

#: 证据片段里标的页码：「…」p24）」。
_PAGE_RE = re.compile(r"p(\d+)")


def _clean(text: str) -> str:
    """折成一行。**写进 CSV 的每一格都要过它。**

    ⚠ PDF 正文里天然带硬换行，直接写进 CSV 会落进引号字段里——
    值本身没错，但任何「先 splitlines 再解析」的读法都会把记录切坏
    （`export_input_templates._read` 原来就是这么读的，2026-10-10 修）。
    干净的做法是**写的时候就折成一行**，而不是要求每个读的人都会处理。
    """
    return " ".join((text or "").split())


def split_subitems(outlook_text: str) -> list[tuple[str, str, int]]:
    """把「可能面对的风险」切成 `[(子条目名, 正文, 起始偏移)]`。"""
    at = outlook_text.find("可能面对的风险")
    body = outlook_text[at:] if at >= 0 else outlook_text
    marks = list(_SUBITEM_RE.finditer(body))
    out: list[tuple[str, str, int]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append((m.group(1), body[m.start():end], at + m.start()))
    return out


def _sentences(text: str, item: str, *, limit: int = 2) -> list[str]:
    """从一段文字里挑出属于 `item` 这一项的句子。

    三条闸门，缺一条都会让「像证据的东西」混进来：

      · **表格行不要**。年报的「研发投入情况」是一整张表，切句后成一大块，
        里面含「营业收入」之类的词——`looks_like_table_row` 是现成的判据。
      · **两组词都要命中**，见 `ITEM_PATTERN`。
      · **数字占比不能太高**，那是表格没被上面那条拦住的漏网。
    """
    from app.parsing.claims import looks_like_table_row

    want, also = ITEM_PATTERN[item]
    out: list[str] = []
    for raw in _SPLIT_RE.split(text):
        s = _SUBHEAD_RE.sub("", raw.strip()).strip()
        if len(s) < 15 or looks_like_table_row(s):
            continue
        if not (any(w in s for w in want) and any(w in s for w in also)):
            continue
        if sum(ch.isdigit() for ch in s) / len(s) > 0.3:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _body_sentences(text: str, *, limit: int = 2) -> list[str]:
    """标题已经点明主题的子条目：正文直接当证据，只过表格与数字两道闸门。"""
    from app.parsing.claims import looks_like_table_row

    out: list[str] = []
    for raw in _SPLIT_RE.split(text):
        s = _SUBHEAD_RE.sub("", raw.strip()).strip()
        if len(s) < 15 or looks_like_table_row(s):
            continue
        if sum(ch.isdigit() for ch in s) / len(s) > 0.3:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def build_candidates(con, project_id: str, period: str) -> dict[str, dict]:
    """一年里四项各自的候选片段。返回的每一项都可能为空（捞不到）。"""
    rows = con.execute(
        "SELECT s.kind, s.page_from, s.text FROM mdna_section s"
        " JOIN file f ON f.file_id = s.file_id"
        " WHERE f.project_id = ? AND f.period = ?"
        " ORDER BY s.page_from, s.section_id",
        (project_id, period),
    ).fetchall()

    per_item: dict[str, list[str]] = {k: [] for k in ITEM_TEXT}
    mitigation: dict[str, str] = {}

    # ---- ① 风险章节 -----------------------------------------------------
    for r in rows:
        if r["kind"] != "outlook":
            continue
        for name, seg, _ in split_subitems(r["text"] or ""):
            if any(x in name for x in SUBITEM_EXCLUDE):
                continue
            targets = [
                pair for key, pairs in SUBITEM_TO_ITEM.items()
                if key in name for pair in pairs
            ]
            if not targets:
                continue
            # 正文去掉缓释措施那段——它单独放 mitigation 列，混在 evidence 里
            # 会让「风险是什么」和「打算怎么办」连成一段读不清。
            mm = _MITIGATION_RE.search(seg)
            risk_part = seg[: mm.start()] if mm else seg
            mit_part = seg[mm.start():] if mm else ""
            for item, need_filter in targets:
                # ⚠ **按项过滤，不整段粘。** 一个子条目可以归多项
                #   （政策及行业风险同时讲需求、价格与原燃料），
                #   整段粘过去会让「需求与钢价」那一格装着原燃料的话——
                #   而它看起来完全像一条真证据。
                #   `need_filter=False` 的走 `_body_sentences`，理由见那张表。
                hits = (
                    _sentences(risk_part, item) if need_filter
                    else _body_sentences(risk_part)
                )
                per_item[item].extend(
                    f"（风险章节「{name}」p{r['page_from']}）{s}" for s in hits
                )
                if hits and mit_part and item not in mitigation:
                    mitigation[item] = _clean(mit_part)[:300]

    # ---- ② 主营业务分析（风险章节没给这项证据时）-------------------------
    for r in rows:
        if r["kind"] != "business_review":
            continue
        for item in ITEM_TEXT:
            if per_item[item]:
                continue
            hits = _sentences(r["text"] or "", item)
            per_item[item].extend(
                f"（**不在风险章节**，「主营业务分析」p{r['page_from']}）{s}"
                for s in hits
            )

    return {
        item: {
            "risk_object": ITEM_TEXT[item][0],
            "impact_path": ITEM_TEXT[item][1],
            "evidence": _clean(" ｜ ".join(dict.fromkeys(per_item[item]))),
            "mitigation": mitigation.get(item, ""),
            "source_page": _first_page(per_item[item]),
        }
        for item in ITEM_TEXT
    }


def _first_page(passages: list[str]) -> int | None:
    """证据里标的第一处页码，落到 `source_page` 列。

    ⚠ 这一列不是装饰：指数下钻与证据面板的「来源页码」读的就是它。
    页码只写在 evidence 的正文里（「（风险章节「环保风险」p24）」）的话，
    页面上那一栏是空的，而**点回原文的入口就断了**。
    """
    for p in passages:
        m = _PAGE_RE.search(p)
        if m:
            return int(m.group(1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="抽 R 候选证据")
    ap.add_argument("--project", default="p-000959")
    ap.add_argument("--write", action="store_true", help="写库（默认只打印）")
    ap.add_argument("--full", action="store_true", help="打印完整 evidence")
    args = ap.parse_args()

    con = connect(DB_PATH)
    try:
        status = {
            (r["period"], r["item"]): r["conclusion"]
            for r in con.execute(
                "SELECT period, item, conclusion FROM risk_disclosure_check"
                " WHERE project_id = ?",
                (args.project,),
            )
        }
        declared = [
            r["value"] for r in con.execute(
                "SELECT value FROM json_each("
                "  (SELECT fiscal_years FROM project WHERE project_id = ?))"
                " ORDER BY value",
                (args.project,),
            )
        ]
        years = sorted(set(declared) | {p for p, _ in status})
        # 一个年度算「已判过」的条件是**四项都有结论**——只判了一项就跳过整年，
        # 会让剩下三项永远空着，而页面上看不出是哪一种。
        todo = [
            y for y in years
            if any(status.get((y, item), "pending") == "pending" for item in ITEM_TEXT)
        ]
        print(
            f"{args.project}：{len(years)} 个年度；"
            f"四项都有结论的年度 {len(years) - len(todo)} 个，待填 {len(todo)} 个：{todo}\n"
        )

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        updates: list[tuple] = []
        for period in todo:
            cand = build_candidates(con, args.project, period)
            for item in ITEM_TEXT:
                c = cand[item]
                has = bool(c["evidence"])
                mark = "有候选" if has else "**空白**"
                print(f"  {period} {item:22s} {mark}")
                if args.full or not has:
                    print(f"       evidence: {c['evidence'][:200] or '（年报里找不到该项证据）'}")
                if has:
                    updates.append(
                        (c["risk_object"], c["impact_path"], c["evidence"],
                         c["mitigation"] or None, c["source_page"],
                         args.project, period, item)
                    )

        filled = len({(u[6], u[7]) for u in updates})
        print(f"\n可填 {filled} / {len(todo) * 4} 格；其余留空交会计按 insufficient 判。")
        if not args.write:
            print("（dry-run：加 --write 写库）")
            return 0

        with con:
            for (risk_object, impact_path, evidence, mitigation,
                 source_page, pid, period, item) in updates:
                row_id = f"r-{pid}-{period}-{item}"
                exists = con.execute(
                    "SELECT 1 FROM risk_disclosure_check WHERE project_id = ?"
                    " AND period = ? AND item = ?",
                    (pid, period, item),
                ).fetchone()
                if exists:
                    # ⚠ **只动 `conclusion = 'pending'` 的行。** 已经有结论的
                    # （如 2022–2024 那十二行）一律不碰——那是会计已经判过的，
                    # 覆盖一次就把判定抹掉了，而且不会报错。
                    con.execute(
                        "UPDATE risk_disclosure_check"
                        " SET risk_object = COALESCE(risk_object, ?),"
                        "     impact_path = COALESCE(impact_path, ?),"
                        "     evidence = COALESCE(evidence, ?),"
                        "     mitigation = COALESCE(mitigation, ?),"
                        "     source_page = COALESCE(source_page, ?)"
                        " WHERE project_id = ? AND period = ? AND item = ?"
                        "   AND conclusion = 'pending'",
                        (risk_object, impact_path, evidence, mitigation,
                         source_page, pid, period, item),
                    )
                else:
                    con.execute(
                        "INSERT INTO risk_disclosure_check"
                        " (id, project_id, period, item, applicable, risk_object,"
                        "  impact_path, evidence, mitigation, conclusion,"
                        "  source_page, reviewer, created_at)"
                        " VALUES (?,?,?,?,1,?,?,?,?,'pending',?,NULL,?)",
                        (row_id, pid, period, item, risk_object, impact_path,
                         evidence, mitigation, source_page, now),
                    )
        print(f"✓ 已写入 {len(updates)} 格候选"
              f"（conclusion 仍是 pending、reviewer 留空——**没有替谁判**）")
        print("  下一步：python scripts/export_input_templates.py --export")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
