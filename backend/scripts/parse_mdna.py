"""从已登记的年报 PDF 里抽出正文，落进 `document_page` 与 `mdna_section`。

用法：
    python scripts/parse_mdna.py                 # 全部年报
    python scripts/parse_mdna.py --project p-600019
    python scripts/parse_mdna.py --force         # 先清掉旧的再重来

## 为什么要有这一步

`parse_reports.py` 只落**财务事实**（数字），正文一个字都没入库。于是
`mdna_section` / `document_page` 一直是空表，系统能算管理层报出的数，
却读不到管理层说的话——而「叙事与事实是否一致」正是这个项目要做的事。

## 白捡的一个收获

`document_page` 上有三个触发器同步 `page_fts`（见 `schema.sql`）。所以这个脚本
一跑，**检索层第一次有东西可搜**：`app/retrieval/fts.py` 此前查任何词都是 0 条，
而且它返回 0 条的样子和「年报里没写这件事」一模一样。

## 幂等

按 `file_id` 先删后插。重复跑不会累积重复行——不去重的话，
同一页会以不同 `page_id` 存成多行，FTS 命中数翻倍，而页面上看不出异常。
删除会经触发器同步清掉 FTS 索引，所以不用手工 rebuild。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _console  # noqa: E402  (与本文件同目录)

_console.setup()

import pymupdf  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import connect, default_db_path  # noqa: E402
from app.parsing.mdna import (  # noqa: E402
    Section,
    paragraphs,
    read_mdna,
    split_subsections,
)
from app.parsing.reader import read_page  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _page_text(doc: pymupdf.Document, index: int) -> str:
    return "\n".join(ln.display_text for ln in read_page(doc[index]) if ln.display_text.strip())


def ingest(con, file_id: str, pdf_path: Path) -> tuple[int, list[str], str | None]:
    """处理一份 PDF。返回 (页数, 子段标题列表, 失败原因)。"""
    if not pdf_path.exists():
        # 演示项目（`scripts/seed_demo.py`）的事实是脚本合成的，**本来就没有 PDF**。
        # 和「文件被删了」混在一起报成失败，会让人以为解析坏了并去查一个不存在的问题。
        if pdf_path.parent.name == "demo":
            return 0, [], "演示数据无对应 PDF（正常，跳过）"
        return 0, [], f"文件不存在：{pdf_path}"

    doc = pymupdf.open(pdf_path)
    try:
        # 先删旧行。触发器会把 page_fts 里对应的索引一并清掉。
        con.execute("DELETE FROM mdna_section WHERE file_id=?", (file_id,))
        con.execute("DELETE FROM document_page WHERE file_id=?", (file_id,))

        n_page = 0
        for i in range(doc.page_count):
            text = _page_text(doc, i)
            con.execute(
                "INSERT INTO document_page"
                " (page_id, file_id, page_no, printed_page_no, text, text_source,"
                "  has_table, image_path, width_pt, height_pt)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"pg-{file_id}-{i + 1}", file_id, i + 1,
                    # 印刷页码留空：它印在页脚，而 read_page 默认把页脚剔掉了。
                    # 硬填一个物理页序进去会更糟——那个字段存在的意义就是
                    # 「和物理页序不一样」，填成一样等于假装它对过了。
                    None,
                    text, "native",
                    0, None,
                    float(doc[i].rect.width), float(doc[i].rect.height),
                ),
            )
            n_page += 1

        sec = read_mdna(doc)
        if sec is None:
            return n_page, [], "未定位到管理层讨论与分析章节"

        # ⚠ **一段跨几页就存几行**，`page_from = page_to = 该页`。
        #
        # 不这么存的话，页码在入库那一刻就丢了：整段正文拼成一个 text 落库，
        # 出库时无从知道哪句话在哪一页，只能给全段盖一个「起始页」。
        # 而那个页码是证据面板里用户真要去翻的东西——他会翻到一页无关的表，
        # 然后不再信任这个系统的任何一条出处。存细一点，代价只是多几行。
        titles: list[str] = []
        n_row = 0
        for sub in split_subsections(sec):
            titles.append(sub.heading)
            by_page: dict[int, list[str]] = {}
            for page_no, line in sub.lines:
                if line.strip():
                    by_page.setdefault(page_no, []).append(line)
            for page_no, lines in sorted(by_page.items()):
                con.execute(
                    "INSERT INTO mdna_section"
                    " (section_id, file_id, heading, kind, page_from, page_to, text)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        f"md-{file_id}-{n_row}", file_id,
                        sub.heading, sub.kind, page_no, page_no, "\n".join(lines),
                    ),
                )
                n_row += 1
        return n_page, titles, None
    finally:
        doc.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="抽取年报正文与 MD&A 章节")
    ap.add_argument("--project", help="只处理某个项目；默认全部")
    ap.add_argument("--force", action="store_true", help="先清空这两张表再重来")
    args = ap.parse_args()

    settings = get_settings()
    data_root = settings.data_root

    con = connect()
    try:
        if args.force:
            con.execute("DELETE FROM mdna_section")
            con.execute("DELETE FROM document_page")
            con.commit()
            print("✓ 已清空 document_page 与 mdna_section")

        sql = (
            "SELECT file_id, project_id, period, rel_path FROM file"
            " WHERE role='annual_report' AND parse_status='parsed'"
        )
        params: tuple = ()
        if args.project:
            sql += " AND project_id=?"
            params = (args.project,)
        sql += " ORDER BY project_id, period"

        files = con.execute(sql, params).fetchall()
        if not files:
            print("✗ 没有已登记的年报。先跑 python scripts/parse_reports.py")
            return 1

        n_pages = n_secs = n_fail = 0
        failures: list[tuple[str, str]] = []
        for row in files:
            pdf = data_root / row["rel_path"]
            pages, titles, err = ingest(con, row["file_id"], pdf)
            n_pages += pages
            n_secs += len(titles)
            if err:
                failures.append((Path(row["rel_path"]).name, err))
                if "正常，跳过" not in err:
                    n_fail += 1
            else:
                print(
                    f"  {Path(row['rel_path']).name[:34]:36} "
                    f"{pages:>4} 页  MD&A {len(titles):>2} 段  首段 {titles[0][:20] if titles else '—'}"
                )
        con.commit()

        n_fts = con.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0]
        print()
        print(f"✓ 页面 {n_pages} 张、MD&A 子段 {n_secs} 段，全文索引 {n_fts} 条")

        # 抽一句话出来自证：不是只看行数，而是看抽出来的东西像不像年报正文。
        sample = con.execute(
            "SELECT heading, page_from, page_to, text FROM mdna_section"
            " WHERE kind='outlook' ORDER BY file_id, page_from LIMIT 1"
        ).fetchone()
        if sample:
            print(f"\n前瞻段抽样「{sample['heading']}」p{sample['page_from']}：")
            # 打印切句结果而不是原文：下游（主张抽取）拿到的就是句子，
            # 切错了在这里就能看见，而不是等到界面上主张全是半句话才发现。
            for p in paragraphs(
                Section(
                    heading=sample["heading"], kind="outlook",
                    page_from=sample["page_from"], page_to=sample["page_to"],
                    lines=tuple(
                        (sample["page_from"], ln) for ln in sample["text"].split("\n")
                    ),
                )
            )[:2]:
                print(f"    p{p.page_no}  {p.display[:70]}")

        if failures:
            header = f"⚠ {n_fail} 份没能抽出 MD&A：" if n_fail else "跳过（正常）："
            print(f"\n{header}")
            for name, why in failures:
                print(f"    {name[:40]:42} {why}")

        print(f"\n数据库：{default_db_path()}")
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
