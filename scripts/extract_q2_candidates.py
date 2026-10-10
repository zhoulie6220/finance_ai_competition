"""从年报里把 Q2 需要的账龄数据抽成**候选**，供会计复核。

用法：
    python scripts/extract_q2_candidates.py                    # 只打印（dry-run）
    python scripts/extract_q2_candidates.py --selftest         # 拿已核验的年份回验判据
    python scripts/extract_q2_candidates.py --write            # 写库

## ⚠ 候选 ≠ 已核验

写进去的每一行都保持 `status = 'pending'`、`reviewer` **留空**——
不替任何人签字。会计复核后自己改成 `validated` 并填复核人，
再走 `scripts/export_input_templates.py --import` 入库。

「没抄」和「抄了但没核」在库里必须长得不一样，所以候选值一律带一条
`note` 说明出处是程序抽取。

## 为什么写库而不是只改 CSV

`人工录入/*.csv` 是**导出物**：`--export` 会按库里的内容整份重写。
只改 CSV 的话，下一次导出就把候选抹掉了，而且不报错。
所以先入库、再从库里导出，两边一致。

（`--import` 反过来会**丢掉 pending 行**——那是对的，不损失任何人的判定；
代价就是候选值必须从库里长出来，不能靠 CSV 往返。）

## 判据与自检

表在**哪一页**沿用 `scripts/locate_aging_tables.py`（它已经能挑出
「应收账款」名下那张，避开预付款项 / 其他应收款两张长得很像的）。

数值的判据是**账龄恒等式**，逐行验：

    合计  ==  1年以内  +  (1至2年 + 2至3年 + 3年以上)

⚠ `3年以上` 是**小计**，不是并列段。有几年（首钢 2015/2019–2022）它和
`3至4年`/`4至5年` 同时印出来，三段直接相加会**重复计数**；而首钢 2023
的 `3至4年` 那一行只印了**期初**列的数，读进来是把期初当期末。
所以口径统一成：**有 `3年以上` 就用它，没有才用下面几段相加。**

恒等式不成立的那一年**拒绝写入并报出来**——不猜、不修。
`--selftest` 拿人工已抄的 2022–2024 回验这套判据，逐字对上才算过。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402

_console.setup()

from app.db.session import connect  # noqa: E402

from locate_aging_tables import _aging_header_pos, _locate  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
DB_PATH = BACKEND_DIR / "var" / "finance.db"

#: 账龄区间标签 → 规范名。**顺序有讲究**：长的、含小计的先看。
LABELS: tuple[tuple[str, str], ...] = (
    ("1年以内小计", "within1"),
    ("1年以内(含1年)", "within1"),
    ("1年以内（含1年）", "within1"),
    ("1年以内", "within1"),
    ("1至2年", "1to2"), ("1-2年", "1to2"), ("1—2年", "1to2"),
    ("2至3年", "2to3"), ("2-3年", "2to3"), ("2—3年", "2to3"),
    ("3至4年", "3to4"), ("3-4年", "3to4"),
    ("4至5年", "4to5"), ("4-5年", "4to5"),
    ("5年以上", "over5"),
    ("3年以上", "over3"),
    ("合计", "total"),
)

_NUM = re.compile(r"(\d[\d,]*\.\d{2})")


def _label_of(line: str) -> str | None:
    """这一行开头是不是一个账龄区间标签。"""
    for text, key in LABELS:
        if line.startswith(text):
            return key
    return None


def _first_number(line: str) -> Decimal | None:
    m = _NUM.search(line)
    if m is None:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except Exception:  # noqa: BLE001
        return None


#: 解析用的**窄**表头。
#:
#: ⚠ 它刻意比 `locate_aging_tables.AGING_HEADERS` 窄得多，**两个用途别混**：
#:
#:   · 那一份是用来「**定位到哪一页**」的 → 宽一点更好，漏了会找不到表；
#:   · 这一份是用来「**从哪一行开始读**」的 → 宽一点会读到**别的表**。
#:
#: 实测首钢 2019 的 p107：一张页上三张表都有账龄标签。
#: 宽的锚点里有个「账龄情况」之类的词命中得很早，于是从那张
#: **「按组合计提坏账准备」**的表开始读，读出 866,953,233.81 ——
#: 恒等式居然也成立（它自己也是 1年以内 + 1年以上 = 自己的合计），
#: 只是那不是应收账款的**账面余额**，少了单项计提那 8,401,011.82。
#: **有数字、有页码、恒等式通过，而数是错的。**
_ANCHORS: tuple[str, ...] = (
    "按账龄分析法计提坏账准备的应收账款",
    "按账龄披露",
    "按账龄列示",
)


def parse_aging_blocks(page_text: str) -> tuple[list[dict[str, Decimal]], bool]:
    """把原文里的账龄数据切成**一个个以「合计」收尾的块**。

    返回 `(块列表, 是否用窄表头锚定过)`。第二个值决定调用方怎么挑：
    锚定过就取第一个通过恒等式的块；没锚定（表头在上一页）就得**唯一**，
    多个候选一律拒绝——见 `_ANCHORS` 那段里的那次实测。

    ⚠ **不能「扫到第一个合计就停」。** 同一页上常常并排放着好几张表，
    每张都有自己的合计。
    """
    at = -1
    for header in _ANCHORS:
        at = max(at, page_text.rfind(header))
    anchored = at >= 0
    body = page_text[at:] if anchored else page_text

    blocks: list[dict[str, Decimal]] = []
    cur: dict[str, Decimal] = {}
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        key = _label_of(line)
        if key is None:
            continue
        value = _first_number(line)
        if value is None:
            # 「1年以内分项」这种只有标题没有数的行——跳过，不是错误
            continue
        if key == "total":
            if cur:
                cur["total"] = value
                blocks.append(cur)
                cur = {}
            continue
        # **先到先得**：同一标签重复出现时（1年以内 vs 1年以内小计）
        # 取第一个；两者不一致的话恒等式会把它挑出来。
        cur.setdefault(key, value)
    return blocks, anchored


def _parts(b: dict[str, Decimal]) -> list[tuple[str, Decimal]]:
    """1 年以上的各段，口径见模块头。"""
    seq: list[tuple[str, Decimal]] = []
    if "1to2" in b:
        seq.append(("1至2年", b["1to2"]))
    if "2to3" in b:
        seq.append(("2至3年", b["2to3"]))
    if "over3" in b:
        seq.append(("3年以上", b["over3"]))
    else:
        for key, name in (("3to4", "3至4年"), ("4to5", "4至5年"), ("over5", "5年以上")):
            if key in b:
                seq.append((name, b[key]))
    return seq


def over_one_year(b: dict[str, Decimal]) -> tuple[Decimal | None, str]:
    """1 年以上的合计，以及它是哪几段相加来的（写进 source_text）。"""
    seq = _parts(b)
    if not seq:
        return None, ""
    return sum((v for _, v in seq), Decimal(0)), " + ".join(n for n, _ in seq)


class Corpus:
    """按 (项目, 年度) 取账龄表的原文。"""

    def __init__(self, con) -> None:
        self.con = con
        self._text: dict[tuple[str, str, int], str] = {}
        self._located: dict[tuple[str, str], dict] = {}
        for r in _locate(con):
            self._located[(r["project_id"], r["period"])] = r

    def consolidated_pages(self, project_id: str, period: str) -> list[tuple[int, str]]:
        """合并口径的账龄页，按页码顺序返回 `[(页码, 原文)]`。

        ⚠ 只要 **合并** 口径的那几页。同一份年报里合并与母公司的账龄表
        长得几乎一样，会计口径 §2.1 要求统一取合并——拿错了数字会全错，
        而且不会报错。
        """
        info = self._located.get((project_id, period))
        if info is None:
            return []
        pages = sorted(
            p["page_no"] for p in info["pages"] if p.get("scope") == "合并"
        )
        out: list[tuple[int, str]] = []
        for page_no in pages:
            if (project_id, period, page_no) not in self._text:
                row = self.con.execute(
                    "SELECT d.text FROM document_page d JOIN file f"
                    " ON f.file_id = d.file_id"
                    " WHERE f.project_id = ? AND f.period = ? AND d.page_no = ?",
                    (project_id, period, page_no),
                ).fetchone()
                self._text[(project_id, period, page_no)] = (
                    (row["text"] if row else "") or ""
                )
            out.append((page_no, self._text[(project_id, period, page_no)]))
        return out

    def aging_figures(self, project_id: str, period: str):
        """返回 `(各段, 页码)`，取不到就 `(None, None)`。

        挑选规则（两条都是实测撞出来的）：

        1. **逐页判，不把几页拼起来扫。** 首钢 2022 定位到 p125、p126 两页，
           而 p125 是**上一张表**的结尾。拼起来只取第一个合计，
           拿到的是上一张表的数。
        2. **锚定过的页取第一个通过恒等式的块；没锚定的页要求唯一。**
           「按账龄披露」这类窄表头之后的第一个块才是应收账款的全额；
           同一页没有窄表头时（表头在上一页）无法区分是哪张表，
           **多于一个候选就拒绝**——宁可让会计手抄，也不能挑一个像模像样的。
        """
        for page_no, text in self.consolidated_pages(project_id, period):
            cands = []
            for b in parse_aging_blocks(text)[0]:
                one, _ = over_one_year(b)
                gross, within1 = b.get("total"), b.get("within1")
                if gross is None or one is None or within1 is None:
                    continue
                if gross == within1 + one:
                    cands.append(b)
            if not cands:
                continue
            if len(cands) == 1:
                return cands[0], page_no
            # 多个候选：**只有当它们给出同一组数时才算不歧义**
            # （同一页上「1年以内」与「1年以内小计」各成一块是常见写法）
            first = cands[0]
            one0, _ = over_one_year(first)
            same = all(
                b.get("total") == first.get("total")
                and over_one_year(b)[0] == one0
                for b in cands[1:]
            )
            if same:
                return first, page_no
            # 歧义 → 记下来，继续看下一页；都不行就拒绝
        return None, None

    def revenue_raw(self, project_id: str, period: str):
        """营业收入原值（元）。取 `value_raw`——它在库里保持着年报印的那个数，
        `value_millions` 是除以 1e6 之后的，换算回来会掉几位小数。"""
        return self.con.execute(
            "SELECT value_raw, source_page FROM financial_fact"
            " WHERE project_id = ? AND metric_key = 'revenue' AND period = ?"
            " AND raw_unit = '元' ORDER BY source_page LIMIT 1",
            (project_id, period),
        ).fetchone()


def selftest(con, project_id: str) -> int:
    """拿**人工已核验**的年份回验判据。这才是这套解析的验收方式。"""
    corpus = Corpus(con)
    known = con.execute(
        "SELECT period, receivable_gross, over_one_year FROM q2_aging"
        " WHERE project_id = ? AND status <> 'pending' ORDER BY period",
        (project_id,),
    ).fetchall()
    if not known:
        print("（库里没有已核验的 Q2 行，无法回验）")
        return 0
    print("回验（人工已抄且已核验的年份）：")
    bad = 0
    for r in known:
        b, page_no = corpus.aging_figures(project_id, r["period"])
        if not b:
            print(f"  {r['period']}: 没定位到能通过恒等式的合并账龄表  ✗")
            bad += 1
            continue
        one, _ = over_one_year(b)
        want_g = Decimal(str(r["receivable_gross"]))
        want_o = Decimal(str(r["over_one_year"]))
        ok = b.get("total") == want_g and one == want_o
        bad += 0 if ok else 1
        print(
            f"  {r['period']}: p{page_no} 余额 {b.get('total')} vs {want_g}；"
            f"1年以上 {one} vs {want_o}  {'✓' if ok else '✗ **对不上**'}"
        )
    if bad:
        print(f"\n✗ {bad} 个年度对不上——**判据不可信，不要拿它去抽 2015–2021**。")
    else:
        print("\n✓ 判据在已核验的年份上逐字对上，可以拿去抽待填的年度。")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="抽 Q2 账龄候选")
    ap.add_argument("--project", default="p-000959")
    ap.add_argument("--write", action="store_true", help="写库（默认只打印）")
    ap.add_argument("--selftest", action="store_true", help="只回验判据，不抽")
    args = ap.parse_args()

    con = connect(DB_PATH)
    try:
        if args.selftest:
            return selftest(con, args.project)

        corpus = Corpus(con)
        status_by_year = {
            r["period"]: r["status"]
            for r in con.execute(
                "SELECT period, status FROM q2_aging WHERE project_id = ?",
                (args.project,),
            )
        }
        # ⚠ **待填的年度要从「项目声明的年度」展开，不能只取库里已有行。**
        # `--import` 会丢掉 pending 行（那是对的，不损失任何人的判定），
        # 于是「还没人去抄」在库里**根本没有痕迹**——只取已有行的话，
        # 首钢只剩 2022–2024 三行，脚本会报「没有可写的候选」。
        # 与 `matching._load_q2_aging` 同一套口径：**并集**。
        declared = [
            r["value"] for r in con.execute(
                "SELECT value FROM json_each("
                "  (SELECT fiscal_years FROM project WHERE project_id = ?))"
                " ORDER BY value",
                (args.project,),
            )
        ]
        years = sorted(set(declared) | set(status_by_year))
        if not years:
            print(f"✗ {args.project} 既没有 fiscal_years 也没有 q2_aging 行")
            return 1

        todo = [y for y in years if status_by_year.get(y) != "validated"]
        print(
            f"{args.project}：{len(years)} 个年度，已核验 "
            f"{len(years) - len(todo)} 个，待填 {len(todo)} 个：{todo}\n"
        )

        updates: list[tuple] = []
        failed: list[str] = []
        for period in todo:
            # ★ 逐页试，**恒等式是唯一的裁判**——不成立就换下一页，
            #   全都不成立就留空。宁可留空让会计去抄，也不能塞一个
            #   看起来像模像样、实际取错表的数进去。
            b, page_no = corpus.aging_figures(args.project, period)
            if not b:
                failed.append(
                    f"{period}（合并口径的账龄表没一页能通过恒等式"
                    f"「合计 = 1年以内 + 1年以上」）"
                )
                continue
            one, parts = over_one_year(b)
            gross, within1 = b["total"], b["within1"]
            rev = corpus.revenue_raw(args.project, period)
            if rev is None or not rev["value_raw"]:
                failed.append(f"{period}（库里没有以「元」记的营业收入原值）")
                continue
            revenue = Decimal(str(rev["value_raw"]))
            detail = "；".join(
                [f"1年以内(含1年) {within1:,}"]
                + [f"{n} {v:,}" for n, v in _parts(b)]
            )
            src = (
                f"账龄：{detail}；合计 {gross:,}。营业收入 {revenue:,}。"
            )
            note = (
                f"程序从年报自动抽取的**候选**（{period} 年报，PDF 物理 p{page_no}）。"
                f"1年以上 = {parts}。"
                f"**尚未人工复核**——请核对原文后把 status 改成 validated 并填 reviewer。"
            )
            print(
                f"  {period}  p{page_no}  余额 {gross:,}  1年以上 {one:,}"
                f"（{parts}）  营收 {revenue:,}"
            )
            updates.append(
                (
                    str(gross), str(one), str(revenue), page_no, src, note,
                    args.project, period,
                )
            )

        if failed:
            print(f"\n⚠ {len(failed)} 个年度没能自动填，留给会计手抄：")
            for f in failed:
                print(f"    {f}")
        if not updates:
            print("\n没有可写的候选。")
            return 0
        if not args.write:
            print(f"\n（dry-run：{len(updates)} 个年度可填。加 --write 写库）")
            return 0

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with con:
            for u in updates:
                (
                    gross, one, revenue, page_no, src, note, project_id, period,
                ) = u
                row_id = f"q2-{project_id}-{period}-consolidated"
                if period in status_by_year:
                    con.execute(
                        "UPDATE q2_aging SET receivable_gross = ?, over_one_year = ?,"
                        " revenue = ?, source_page = ?, source_text = ?, note = ?"
                        " WHERE project_id = ? AND period = ? AND scope = 'consolidated'",
                        (gross, one, revenue, page_no, src, note, project_id, period),
                    )
                else:
                    # 库里连这一行都没有——补一行 pending。
                    # ⚠ status 必须是 pending：值是我们抽的，**没人核过**。
                    con.execute(
                        "INSERT INTO q2_aging (id, project_id, period, scope,"
                        " receivable_gross, over_one_year, revenue, source_page,"
                        " source_text, status, note, reviewer, created_at)"
                        " VALUES (?,?,?,'consolidated',?,?,?,?,?,'pending',?,NULL,?)",
                        (row_id, project_id, period, gross, one, revenue,
                         page_no, src, note, now),
                    )
        print(
            f"\n✓ 已写入 {len(updates)} 个年度的候选"
            f"（status 仍是 pending、reviewer 留空——**没有替谁签字**）"
        )
        print(
            "  下一步：python scripts/export_input_templates.py --export"
            "  → 把 人工录入/Q2_账龄.csv 交给会计复核"
        )
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
