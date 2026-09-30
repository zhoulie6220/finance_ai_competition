"""在年报正文里定位「应收账款账龄表」，输出给会计同学抄录的清单。

用法：
    python scripts/locate_aging_tables.py
    python scripts/locate_aging_tables.py --csv 账龄表位置.csv

## 这是会计口径 gap_closure_v1.0 §2.5 派给「计算机同学」的那一步

> 计算机同学先在年报附注中搜索：应收账款、账龄、1年以内、1至2年、2至3年、
> 坏账准备。会计同学逐年抄录表格并截图或标注页码。

**定位是机械活，抄录不是**——账龄表的列名各家写法不一、
「1年以上」可能是直接一行也可能是五个区间相加、还有合并/母公司口径之分。
所以这一层只负责「在哪一页」，把判断留给人。

## 为什么要挑出「应收账款」那张

同一份年报里带「账龄」的表至少有三张：预付款项账龄、其他应收款账龄、
应收账款账龄。后面两张长得很像，**拿错了不会报错**，只会让 Q2 的分子分母
全部用错数据。所以判据要卡到「应收账款」章节下面。

输出里带上片段，人一眼就能看出是不是找对了。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
# _console 在 backend/scripts/ 下，两个脚本共用同一份编码兜底。
# 复制一份到这里的话，改了一处忘了另一处，两边都不会报错。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "scripts"))

# _console 与本文件同目录。**这一行不能省**：
# 直接 `python scripts/x.py` 时 Python 会自动把脚本目录放进 sys.path，
# 但测试用 `spec_from_file_location` 按路径加载脚本时**不会**——
# 少了它，`import _console` 只在跑测试时炸，看起来像测试坏了。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402  (与本文件同目录)

_console.setup()

from app.db.session import connect  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
DB_PATH = BACKEND_DIR / "var" / "finance.db"

#: 账龄表必须有的词。
HEADER_MARKERS = ("账龄",)

#: 期间列的表头写法。**必须列全**——老年报用「年末/年初」，
#: 新的是「期末/期初」。只认「期末」的话 2015–2017 那几份会被判成
#: 「年报未披露账龄表」，而会计据此记 unavailable_disclosure ——
#: **漏报比误报更糟**：误报会计一眼能看出来，漏报他不会去查。
PERIOD_MARKERS = (
    "期末账面余额", "年末账面余额", "期末余额", "年末余额",
    "期末数", "年末数", "期末金额", "年末金额",
    # 有些年份的账龄表列名只写「账面余额」，不带期间前缀（实测首钢 2022）
    "账面余额",
)

#: 余额类的表头词，至少要有一个。
HEADER_MARKERS_ANY = ("账面余额", "账面价值", "坏账准备", "余额")

#: 「应收账款」的章节标识。各家写法不同，列全一些。
AR_MARKERS = ("应收账款",)
#: 明确不是应收账款的账龄表——命中这些就排除。
EXCLUDE_MARKERS = ("预付款项", "预付款项", "其他应收款")

#: 账龄表头的写法。各家年报不统一，列全一些。
AGING_HEADERS = (
    "按账龄披露", "按账龄列示", "账龄情况", "账龄列示",
    "应收账款的账龄", "应收账款账龄如下", "应收账款按账龄",
    "按账龄分析法计提坏账准备的应收账款", "账龄如下",
)


def _aging_header_pos(text: str) -> int | None:
    """账龄表头在这一页里的位置。找不到返回 None。"""
    positions = [text.find(h) for h in AGING_HEADERS]
    found = [p for p in positions if p >= 0]
    return min(found) if found else None


#: 账龄区间词。至少命中一个才认。
AGE_BUCKETS = ("1年以内", "1 年以内", "1至2年", "1 至 2 年", "2至3年", "3年以上")


def main() -> int:
    parser = argparse.ArgumentParser(description="定位应收账款账龄表")
    parser.add_argument("--csv", default=None, help="把结果写成 CSV（可选）")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"✗ 数据库不存在：{DB_PATH}", file=sys.stderr)
        return 1

    con = connect(DB_PATH)
    try:
        rows = _locate(con)
    finally:
        con.close()

    _report(rows)
    if args.csv:
        _write_csv(rows, Path(args.csv))
    return 0


def _locate(con) -> list[dict]:
    """逐份年报找账龄表。"""
    projects = {
        r["file_id"]: (r["project_id"], r["period"], r["rel_path"])
        for r in con.execute(
            "SELECT file_id, project_id, period, rel_path FROM file"
        )
    }

    # 只取含「账龄」的页，减少扫描量（209 页而不是 3547 页）
    pages = con.execute(
        "SELECT page_id, file_id, page_no, text FROM document_page"
        " WHERE text LIKE '%账龄%' ORDER BY file_id, page_no"
    ).fetchall()

    found: dict[str, dict] = {}
    for r in pages:
        text = r["text"] or ""
        flat = text.replace(" ", "")

        if not all(m in text for m in HEADER_MARKERS):
            continue
        if not any(m in text for m in PERIOD_MARKERS):
            continue
        if not any(m in text for m in HEADER_MARKERS_ANY):
            continue
        if not any(m in flat for m in AGE_BUCKETS):
            continue

        # ★ **按位置判定，不按整页判定。**
        #
        # 同一页上常常并排放着好几张账龄表：
        #
        #     5. 应收账款  (1)按账龄披露  … 1年以内 …
        #     8. 预付款项  (1)预付款项按账龄列示 … 1年以内 …
        #
        # 整页搜「应收账款」两张都会命中，于是预付款项那张被误收进来。
        # 会计口径要的是**应收账款**那张，拿错了 Q2 的分子分母全错。
        #
        # 所以：先找到账龄表头，**只在它前面一小段里**认章节名——
        # 最近的章节名才算数。
        anchor = _aging_header_pos(text)
        if anchor is None:
            continue
        before = text[max(0, anchor - 220) : anchor].replace(" ", "")

        # ★ **表头在页面最开头时，章节名在上一页。**
        # 实测 2015/2017 两份就是这样：表头出现在第 0–4 个字符，
        # 而「应收账款」在上一页末尾。不往前接一段的话，
        # 这两份会被判成「年报未披露账龄表」——**漏报比误报更糟**，
        # 会计会据此记 unavailable_disclosure 而实际有表。
        if len(before) < 40:
            before = _previous_tail(con, r["file_id"], r["page_no"]) + before

        # ★ **排除词必须紧贴表头才算。**
        # 原来用「谁离得更近谁赢」，结果 首钢 2022 那页里
        # 「应收账款」在 157 位、「其他应收款」在 169 位——后者只是
        # 顺带提了一句（在讲坏账准备披露格式），却把正确的表排除了。
        # 阈值收到 20 字：真正的「其他应收款账龄表」章节名是紧挨着表头的；
        ex = max((before.rfind(m) for m in EXCLUDE_MARKERS), default=-1)
        if ex >= 0 and len(before) - ex < 20:
            continue
        # ★ 表头**自己**也要算。
        # 实测 2015/2017 两份的表头是「按账龄分析法计提坏账准备的应收账款」——
        # 「应收账款」就在表头里，而我只在表头**前面**找，于是漏掉。
        header_text = text[anchor : anchor + 50].replace(" ", "")
        if "应收账款" not in before and "应收账款" not in header_text:
            continue

        info = projects.get(r["file_id"])
        if not info:
            continue
        project_id, period, path = info

        key = f"{project_id}|{period}"
        # 一份年报里可能有好几页（合并 / 母公司 / 续表），全记下来让人挑
        entry = found.setdefault(
            key,
            {
                "project_id": project_id,
                "period": period,
                "file": path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
                "pages": [],
            },
        )
        entry["pages"].append(
            {
                "page_no": r["page_no"],
                "scope": _scope_of(con, r["file_id"], r["page_no"]),
                "snippet": _snippet(text, "账龄"),
            }
        )

    return sorted(found.values(), key=lambda x: (x["project_id"], x["period"]))


def _previous_tail(con, file_id: str, page_no: int, width: int = 300) -> str:
    """上一页的末尾。用于「表头在本页开头、章节名在上一页」的情形。"""
    r = con.execute(
        "SELECT text FROM document_page WHERE file_id = ? AND page_no = ?",
        (file_id, page_no - 1),
    ).fetchone()
    return (r["text"] or "")[-width:].replace(" ", "") if r else ""


def _scope_of(con, file_id: str, page_no: int) -> str:
    """这一页的账龄表属于**合并报表**还是**母公司报表**。

    ⚠ 这一条不是锦上添花。同一份年报里两张账龄表长得几乎一样，
    而会计口径 §2.1 明确要求「统一采用**合并报表**、期末口径」——
    **拿错了数字会全错，而且不会报错**。

    判据是往回找最近的章节标题：「合并财务报表…项目注释」在前面、
    「母公司财务报表主要项目注释」在后面，两者各自管辖自己之后的页。
    """
    rows = con.execute(
        "SELECT page_no, text FROM document_page"
        " WHERE file_id = ? AND page_no <= ? ORDER BY page_no DESC",
        (file_id, page_no),
    ).fetchall()
    for r in rows:
        text = r["text"] or ""
        if "母公司财务报表主要项目注释" in text or "母公司财务报表附注" in text:
            return "母公司"
        if "合并财务报表主要项目注释" in text or "合并财务报表项目注释" in text:
            return "合并"
    return "未知"


def _snippet(text: str, anchor: str, width: int = 90) -> str:
    i = text.find(anchor)
    seg = text[max(0, i - 20) : i + width]
    return seg.replace("\n", " ").replace("  ", " ").strip()


def _report(rows: list[dict]) -> None:
    if not rows:
        print("✗ 没有定位到任何应收账款账龄表。")
        print("  检查：document_page 是否已导入、正文是否含附注。")
        return

    print(f"共在 {len(rows)} 份年报里定位到应收账款账龄表")
    print()
    for r in rows:
        merged = [p for p in r["pages"] if p["scope"] == "合并"]
        parent = [p for p in r["pages"] if p["scope"] == "母公司"]
        other = [p for p in r["pages"] if p["scope"] == "未知"]

        line = f"  {r['project_id']}  {r['period']}  "
        if merged:
            line += "**合并** " + "、".join(f"p{p['page_no']}" for p in merged)
        else:
            line += "⚠ 未识别到合并报表的账龄表"
        if parent:
            line += "   母公司 " + "、".join(f"p{p['page_no']}" for p in parent)
        if other:
            line += "   未识别 " + "、".join(f"p{p['page_no']}" for p in other)
        print(line)
        for p in merged[:2]:
            print(f"      p{p['page_no']}: {p['snippet'][:92]}")
    print()
    missing = _missing(rows)
    if missing:
        print(f"⚠ 没找到账龄表的年报：{'、'.join(missing)}")
        print("  会计口径 §2.1：年报确实没披露时记 unavailable_disclosure，**不得填 0**。")
    else:
        print("✓ 每份年报都找到了账龄表")


def _missing(rows: list[dict]) -> list[str]:
    have = {(r["project_id"], r["period"]) for r in rows}
    out = []
    for pid, period in (
        [("p-600019", str(y)) for y in range(2015, 2025)]
        + [("p-000932", str(y)) for y in range(2022, 2025)]
        + [("p-000959", str(y)) for y in range(2022, 2025)]
    ):
        if (pid, period) not in have:
            out.append(f"{pid}/{period}")
    return out


def _write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["项目", "年度", "年报文件", "口径", "页码", "页码片段"])
        for r in rows:
            for p in r["pages"]:
                w.writerow([
                    r["project_id"], r["period"], r["file"],
                    p.get("scope", "未知"), p["page_no"], p["snippet"],
                ])
    print()
    print(f"已写出 {path}（可直接发给会计同学）")


if __name__ == "__main__":
    raise SystemExit(main())
