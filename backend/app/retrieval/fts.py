"""年报原文检索。

⚠ 核心陷阱：SQLite FTS5 的 `trigram` 分词器**要求查询至少 3 个字符**。
短于 3 字符的 MATCH 会**静默返回 0 条** —— 不报错、不警告，只是查不到。
而中文财务术语有大量两个字的核心词：存货、商誉、营业、费用、成本、收入、
折旧、减值……直接用 FTS 会让这些词永远搜不出来，且看起来像是「年报里没有」。

因此检索一律走 `search_pages()`，不要直接查 page_fts：
    长查询 → FTS（可用 bm25 排序、snippet 摘要）
    短查询 → LIKE 扫描（本项目的年报表规模下是毫秒级）

规模参考：9 份年报约 2250 页、正文合计约 4–5MB。LIKE 全表扫描在这个量级下
约数十毫秒，远未到需要优化的程度。真到了需要优化的规模（比如上百家公司），
应改用「预先切分双字词再入索引」的方案，而不是简单换分词器。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# 低于这个长度必须走 LIKE —— trigram 无法为少于 3 个字符的查询生成词元
MIN_FTS_CHARS = 3

# LIKE 中的通配符需转义，否则用户搜「%」会匹配全部内容
_LIKE_ESCAPE = "\\"


@dataclass(frozen=True)
class PageHit:
    page_id: str
    file_id: str
    page_no: int
    printed_page_no: str | None
    snippet: str
    score: float
    matched_by: str  # 'fts' 或 'like'，便于排查「为什么这条没搜到」


def _escape_like(text: str) -> str:
    return (
        text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


def _search_fts(
    con: sqlite3.Connection, query: str, limit: int
) -> list[PageHit]:
    rows = con.execute(
        """
        SELECT p.page_id, p.file_id, p.page_no, p.printed_page_no,
               snippet(page_fts, 0, '【', '】', '…', 16) AS snippet,
               bm25(page_fts) AS score
        FROM page_fts
        JOIN document_page p ON p.rowid = page_fts.rowid
        WHERE page_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [
        PageHit(
            page_id=r["page_id"],
            file_id=r["file_id"],
            page_no=r["page_no"],
            printed_page_no=r["printed_page_no"],
            snippet=r["snippet"] or "",
            score=float(r["score"]),
            matched_by="fts",
        )
        for r in rows
    ]


def _search_like(
    con: sqlite3.Connection, query: str, limit: int
) -> list[PageHit]:
    pattern = f"%{_escape_like(query)}%"
    rows = con.execute(
        f"""
        SELECT page_id, file_id, page_no, printed_page_no,
               text AS snippet
        FROM document_page
        WHERE text LIKE ? ESCAPE '{_LIKE_ESCAPE}'
        ORDER BY file_id, page_no
        LIMIT ?
        """,
        (pattern, limit),
    ).fetchall()

    hits: list[PageHit] = []
    for r in rows:
        text = r["snippet"] or ""
        idx = text.find(query)
        start = max(0, idx - 30)
        excerpt = text[start : start + 120].replace("\n", " ")
        if start > 0:
            excerpt = "…" + excerpt
        hits.append(
            PageHit(
                page_id=r["page_id"],
                file_id=r["file_id"],
                page_no=r["page_no"],
                printed_page_no=r["printed_page_no"],
                snippet=excerpt,
                score=0.0,  # LIKE 无相关度排序
                matched_by="like",
            )
        )
    return hits


def search_pages(
    con: sqlite3.Connection,
    query: str,
    *,
    file_ids: list[str] | None = None,
    limit: int = 20,
) -> list[PageHit]:
    """按关键词检索年报页面原文。

    自动在 FTS 与 LIKE 之间选择，调用方无需关心查询长度。
    `file_ids` 可限定在指定文件范围内检索。
    """
    q = query.strip()
    if not q:
        return []

    if len(q) >= MIN_FTS_CHARS:
        hits = _search_fts(con, q, limit if file_ids is None else limit * 4)
    else:
        hits = _search_like(con, q, limit if file_ids is None else limit * 4)

    if file_ids is not None:
        allowed = set(file_ids)
        hits = [h for h in hits if h.file_id in allowed][:limit]

    return hits[:limit]
