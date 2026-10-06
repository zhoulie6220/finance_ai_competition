"""`file_id` 必须是**由内容算出来的**。

★ 这一条盯的是一整条静默链：

    file_id 随机  →  section_id = md-{file_id}-{序号} 变
                  →  claim_id = sha1(项目|章节|句序|原文) 变
                  →  **会计填好的 P 表一个编号都对不上**

也就是说，任何队友跑一次 `init_db.py --force` 再重新解析年报，
会计同学逐条填的东西就全废了——而文档里写的却是
「确定性 id，防重跑重复靠的是它」。

2026-10-06 实测撞上：为给 `claim_match` 加几列重建了一次库，
交回来的 121 行**一行都对不上**。当时只能按原句重新编号救回来
（`scripts/export_input_templates.py --rekey`）。

⚠ **这个测试必须能在没有年报 PDF 的情况下跑。** 用临时文件即可——
不然它会被跳过，而跳过的测试和通过的测试在 CI 输出里长得一样。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND / "scripts"))
sys.path.insert(0, str(BACKEND))

import parse_reports  # noqa: E402
from app.db.session import connect_memory, init_schema  # noqa: E402


@pytest.fixture()
def con() -> sqlite3.Connection:
    c = connect_memory()
    init_schema(c)
    c.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code, industry,"
        " fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
        ("p1", "测试", "某钢铁", "600019.SH", "steel", '[]', "t"),
    )
    yield c
    c.close()


def _write_pdf(path: Path, text: str) -> Path:
    """写一份**真的能打开**的最小 PDF。

    ⚠ 不能用 b"%PDF-1.4 ..." 糊弄：`register_file` 要读页数，
    假文件会抛 FileDataError——那时测试红的是「打不开」，
    和它真正要盯的「id 稳不稳定」没有关系，排查方向会被带偏。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def pdf(tmp_path: Path) -> Path:
    return _write_pdf(tmp_path / "宝钢股份：2018年年度报告.pdf", "年报正文")


def test_the_same_pdf_gets_the_same_id_in_a_fresh_database(con, pdf, tmp_path):
    """★ 换一个库重新登记同一份年报，id 必须一样。

    这就是「重建库之后会计的表还能不能用」的全部答案。
    """
    first = parse_reports.register_file(con, "p1", pdf, "samples/a.pdf", "2018")

    # 另起一个库，从头再来一遍
    con2 = connect_memory()
    init_schema(con2)
    con2.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code, industry,"
        " fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
        ("p1", "测试", "某钢铁", "600019.SH", "steel", '[]', "t"),
    )
    second = parse_reports.register_file(con2, "p1", pdf, "samples/a.pdf", "2018")
    con2.close()

    assert first == second, (
        f"两次登记同一份年报拿到了不同的 id（{first} vs {second}）——"
        "claim_id 会跟着变，会计填好的表就作废了"
    )


def test_different_content_gets_a_different_id(con, pdf, tmp_path):
    """反面：内容变了就**必须**换 id。

    少了这条，把 id 写死成一个常量也能让上一条通过。
    """
    first = parse_reports.register_file(con, "p1", pdf, "samples/a.pdf", "2018")
    other = _write_pdf(tmp_path / "b.pdf", "另一份完全不同的年报")
    second = parse_reports.register_file(con, "p1", other, "samples/b.pdf", "2019")
    assert first != second


def test_the_same_bytes_cannot_be_registered_twice_as_two_files(con, pdf):
    """同一份内容在同一个项目里只能是一份文件。

    这条**不是**我这轮加的规则——`file` 表上本来就有
    `UNIQUE (project_id, sha256)`，写在这里是为了让「路径进了哈希」
    这件事有个说清楚的反面：内容相同、路径不同时，哈希会算出不同的 id，
    但数据库会先拦下来。

    ⚠ 也就是说 `register_file` 的早退只看 (项目, 期间, 角色)。
    同一份 PDF 换个年度再登记一次会撞到这条 UNIQUE——
    **报错是对的**（同一份年报不该挂两个期间），但错误信息来自数据库约束，
    不是来自登记函数，读的人要能对得上。
    """
    parse_reports.register_file(con, "p1", pdf, "samples/a.pdf", "2018")
    with pytest.raises(sqlite3.IntegrityError):
        parse_reports.register_file(con, "p1", pdf, "samples/copy/a.pdf", "2019")


def test_registering_twice_in_the_same_database_reuses_the_row(con, pdf):
    """幂等：同一个库里同年重复登记返回原来那条，不插第二行。"""
    a = parse_reports.register_file(con, "p1", pdf, "samples/a.pdf", "2018")
    b = parse_reports.register_file(con, "p1", pdf, "samples/a.pdf", "2018")
    assert a == b
    assert con.execute("SELECT COUNT(*) FROM file").fetchone()[0] == 1
