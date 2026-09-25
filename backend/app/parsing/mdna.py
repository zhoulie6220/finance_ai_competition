"""从年报 PDF 里切出「管理层讨论与分析」章节正文。

这是「叙事」那一半的**取数入口**。此前解析层只写财务事实
（`financial_fact`），正文一个字都没落库，于是 `mdna_section` / `document_page`
两张表一直是空的——系统能算管理层说的数字，却读不到管理层说的话。

## 章节边界靠「第X节」标题，不靠页码

各家的 MD&A 位置差得很远：宝钢 2024 在第三节（p.9），宝钢 2015 在第四节（p.10），
华菱 2024 在第三节（p.11）。**按页号硬编码换一家就崩**，而且崩得不报错——
只是切出来一段前言，关键词一个都命中不了，表现为「这家公司没有叙事」。

所以按标题扫：找到含「管理层讨论与分析」的那一节，往下取到**下一个** `第X节`
出现的那一页为止。旧格式（2015 年那批）叫「董事会报告」，一并认。

## 目录页必须剔掉

年报前几页是目录，密密麻麻列着全部 `第X节`。不剔的话会切出 1 页的假章节。
判据与 `statements.py::_title_lines` 剔报表目录页**同一条**：
一页里出现两个以上不同章节名，那一定是目录。两处用同一个判据是有意的——
下次再遇到「一页列出多个条目」的排版，改一处就得想起另一处。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

from app.parsing.reader import read_page

#: 顶级章节标题：「第三节  管理层讨论与分析」。允许中间有空格（照排常有）。
_SECTION_RE = re.compile(r"^\s*第\s*([一二三四五六七八九十]+)\s*节\s*(.*)$")

#: MD&A 章节的标题关键词。新旧披露格式用词不同：
#:   2021 年版格式 → 「管理层讨论与分析」
#:   更早的格式    → 「董事会报告」
_MDNA_TITLES = ("管理层讨论与分析", "董事会报告", "经营情况讨论与分析")

#: MD&A **内部**的一级子标题：「一、经营情况讨论与分析」
_SUB_RE = re.compile(r"^\s*([一二三四五六七八九十]+)\s*[、.．]\s*(.*)$")

#: 前瞻段的子标题关键词。这些段落讲的是**下一年**，验证对象与回顾段不同——
#: 「2024 年报里说 2025 年要好」要用 2025 年的财务事实去验，不能用 2024 的。
#: 混在一起会让前瞻主张全部误判为「冲突」，而那是系统的核心卖点所在。
_OUTLOOK_MARKERS = ("未来发展", "经营计划", "前景", "展望")

#: 章节标题最长多少字。超过就当它不是标题，是正文里凑巧以「第X节」开头的句子。
_MAX_TITLE_LEN = 24


@dataclass(frozen=True)
class Heading:
    """一个章节标题及其所在页（0-based）。"""

    page_index: int
    title: str


@dataclass(frozen=True)
class Section:
    """切出来的一段正文。

    ⚠ 正文以 `lines`（逐行带页码）为**真身**，`text` 只是它拼出来的样子。
    反过来存（只存一个 text，再在切句时统一盖一个页码）会让整段正文的出处
    全部指向章节起始页——而证据面板上那个页码是用户真要去翻的，
    标错一个页码比没有页码更糟：他翻过去看到的是一页无关的表。
    """

    heading: str
    kind: str                                  # 与 mdna_section.kind 的 CHECK 取值一致
    page_from: int                             # 1-based，与 document_page.page_no 一致
    page_to: int                               # 含
    lines: tuple[tuple[int, str], ...]         # (页码, 行文本)

    @property
    def text(self) -> str:
        return "\n".join(t for _, t in self.lines if t.strip())


@dataclass(frozen=True)
class Paragraph:
    """一句（或一「段」）正文，带页码出处。

    叙事类证据的单位是**句子**而不是行：年报按 40 来个字折行，
    一行往往只是半句话，「同比下降」和它修饰的主语常常不在同一行。
    """

    text: str
    page_no: int

    @property
    def display(self) -> str:
        return re.sub(r"\s+", "", self.text)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def find_headings(doc: pymupdf.Document) -> list[Heading]:
    """扫出全部顶级章节标题。

    ⚠ **目录页会伪装成标题页**。判据是「一页里出现两个以上章节标题」时整页丢掉，
    与 `statements.py::_title_lines` 处理报表目录页的判据一致。
    只看第一个的话，目录页会切出一个 1 页的假章节，而且不报错。
    """
    out: list[Heading] = []
    for i in range(doc.page_count):
        found: list[str] = []
        for ln in read_page(doc[i]):
            m = _SECTION_RE.match(_norm(ln.display_text))
            if m and len(_norm(m.group(2))) <= _MAX_TITLE_LEN:
                found.append(_norm(m.group(2)))
        # 一个页面里只有一个章节标题 → 是章节起始页；两个以上 → 是目录
        if len(found) == 1:
            out.append(Heading(page_index=i, title=found[0]))
    return out


def locate_mdna(doc: pymupdf.Document) -> tuple[int, int, str] | None:
    """定位 MD&A 章节，返回 (起始页下标, 结束页下标不含, 标题)。

    找不到返回 None——**不猜测、不退回全文**。退回全文的后果是拿 200 页附注
    去做「词频分析」，命中的全是财务表格里的词，而结果看起来像那么回事。
    """
    heads = find_headings(doc)
    for k, h in enumerate(heads):
        if any(t in h.title for t in _MDNA_TITLES):
            end = heads[k + 1].page_index if k + 1 < len(heads) else doc.page_count
            # 下一页的起点即本节的终点：标题页本身算本节的第一页，
            # 所以不 +1 也不会把下一节的开头吃进来（下一节的标题在 end 页上）。
            return h.page_index, max(end, h.page_index + 1), h.title
    return None


#: 结构性噪声：标题、编号小节、披露格式里的勾选框。
#:
#: ⚠ 这些行**不以句号结尾**，所以会被 `_split_sentences` 当作「没说完」而
#: 攒进下一句，于是拼出这种句子：
#:
#:     六、公司关于公司未来发展的讨论与分析(一)行业格局和趋势√适用□不适用2022年，钢铁行业…
#:
#: 它是真实年报里**根本没有印过的一句话**，却会作为「年报原句」出现在证据面板上。
#: 所以不是过滤掉就完事——还要在它出现的位置**强制断句**，否则前半句会被
#: 和后边的正文粘起来。
_STRUCTURAL = (
    re.compile(r"^[一二三四五六七八九十]+[、.．]"),         # 一、经营情况讨论与分析
    re.compile(r"^[（(][一二三四五六七八九十]+[)）]"),       # （一）行业格局和趋势
    re.compile(r"^[√□✓✗☑×]+(适用|不适用)"),               # √适用 □不适用
    re.compile(r"^(适用|不适用)[√□✓✗☑×]+"),
)


def _is_structural(text: str) -> bool:
    return any(p.match(text) for p in _STRUCTURAL)


def _split_sentences(lines: list[tuple[int, str]]) -> list[Paragraph]:
    """把「(页码, 行)」切成句子，逐句带上页码。

    ⚠ 折行会让一句话断成两半（「同比下降」可能在下一行）。所以先把**相邻同页**
    的行连起来再切句，否则「2024年公司营业收入」和「同比下降5%」会各成一句，
    两句都不含完整含义，关键词与方向判断同时失效。

    ⚠ 页码必须**逐行记着**，不能切完再回头补。补的话只能记住「这一段从第几页开始」，
    而一句话可能跨页——那时标出来的页码是错的，而它是证据面板里用户要去核对的
    那个数字。证据链错一个页码，比没有证据更糟。
    """
    buf: list[Paragraph] = []
    cur_page: int | None = None
    acc = ""
    for page_no, raw in lines:
        t = _norm(raw)
        if not t:
            continue
        # 标题/勾选框：在**这里**断句，别让它和后边的正文粘成一句。
        # 见 `_STRUCTURAL` 上面那段——粘出来的句子年报上根本没印过。
        if _is_structural(t):
            if acc:
                buf.extend(_flush(acc, cur_page or page_no))
                acc = ""
            continue
        if cur_page is not None and page_no != cur_page:
            # 跨页了：把攒的这段冲掉，页码取起始页
            buf.extend(_flush(acc, cur_page))
            acc = ""
        cur_page = page_no
        acc += t
        # 到句末就冲一段，免得一个 acc 攒成整页
        if acc and acc[-1] in "。；！?":
            buf.extend(_flush(acc, page_no))
            acc = ""
    if acc:
        buf.extend(_flush(acc, cur_page or 1))
    return buf


def _flush(text: str, page_no: int) -> list[Paragraph]:
    out: list[Paragraph] = []
    for piece in re.split(r"(?<=[。；！?])", text):
        piece = piece.strip()
        if piece:
            out.append(Paragraph(text=piece, page_no=page_no))
    return out


def read_mdna(doc: pymupdf.Document) -> Section | None:
    """抽出整个 MD&A 章节。"""
    loc = locate_mdna(doc)
    if loc is None:
        return None
    start, end, title = loc

    lines: list[tuple[int, str]] = []
    for i in range(start, end):
        for ln in read_page(doc[i]):
            if ln.display_text.strip():
                lines.append((i + 1, ln.display_text))

    return Section(
        heading=title,
        kind="mdna",
        page_from=start + 1,
        page_to=end,
        lines=tuple(lines),
    )


def split_subsections(sec: Section) -> list[Section]:
    """把 MD&A 按「一、」「二、…」切成子段，标出哪几段是**前瞻**。

    前瞻段（「公司关于公司未来发展的讨论与分析」「经营计划」）说的是下一年，
    验证对象与回顾段不同。不区分的话，前瞻主张会被拿去和**本年**数字比，
    方向多半对不上，于是全部判成「冲突」——而它们其实只是「还没到时候」。
    """
    marks: list[tuple[int, str, int]] = []   # (行号, 子标题, 页码)
    for idx, (page_no, raw) in enumerate(sec.lines):
        t = _norm(raw)
        m = _SUB_RE.match(t)
        if m and len(t) <= _MAX_TITLE_LEN:
            marks.append((idx, t, page_no))

    # ⚠ **砍掉第一个「一、」之前的东西**。章节标题常常落在页面中间，于是上一个
    #   章节的尾巴（「十二、其他」加上它那几行表格）会被当成 MD&A 的第一段。
    #   实测宝钢 2024 就有这么一段。它的内容是上一节的财务表格，
    #   混进叙事语料后既贡献不了主张，又会让「本章共 N 段」这类统计凭空多一截。
    for k, (_, title, _) in enumerate(marks):
        if _SUB_RE.match(title).group(1) == "一":     # type: ignore[union-attr]
            marks = marks[k:]
            break

    if not marks:
        return [sec]

    out: list[Section] = []
    for k, (idx, title, page_no) in enumerate(marks):
        stop = marks[k + 1][0] if k + 1 < len(marks) else len(sec.lines)
        body = sec.lines[idx:stop]
        kind = "outlook" if any(m in title for m in _OUTLOOK_MARKERS) else "business_review"
        out.append(
            Section(
                heading=title,
                kind=kind,
                page_from=page_no,
                page_to=body[-1][0] if body else page_no,
                lines=body,
            )
        )
    return out


#: 一行里数字字符占比超过这个数，就当它是**表格数据**而不是叙述文字。
#: 实测「单位：万吨7,6001307,4001207,2001107,000…」这类行数字占比在 0.7 以上，
#: 而正常的经营叙述句不到 0.15，中间有一大片空地，阈值取哪都不敏感。
_TABLE_DIGIT_RATIO = 0.35


def _looks_like_table(text: str) -> bool:
    """是不是表格里的一行数字。

    ⚠ 必须滤掉。MD&A 章节里夹着大量财务表格，它们会被 `read_page` 按行读成
    「句子」，里面既有「投产」「产量」这类词，也有年份和千分位数字。
    不过滤的话，「主张」里会混进
    `单位：万吨7,6001307,4001207,2001107,000…` 这种东西，
    而它在证据面板里显示为「年报原句」——**看起来像解析坏了**。
    """
    if not text:
        return True
    digits = sum(c.isdigit() for c in text)
    return digits / len(text) > _TABLE_DIGIT_RATIO


def paragraphs(sec: Section) -> list[Paragraph]:
    """把一个子段切成带页码的句子，供主张抽取用。表格行在这里滤掉。"""
    return [p for p in _split_sentences(list(sec.lines)) if not _looks_like_table(p.display)]


__all__ = [
    "Heading",
    "Paragraph",
    "Section",
    "find_headings",
    "locate_mdna",
    "paragraphs",
    "read_mdna",
    "split_subsections",
]
