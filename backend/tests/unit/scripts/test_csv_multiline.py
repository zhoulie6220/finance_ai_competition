"""CSV 里**引号字段带换行**时必须解析正确。

★ 这是一个会**静默损坏数据**的老 bug（2026-10-10 修）。

`_read()` 原来长这样：

    text = path.read_text(encoding="utf-8-sig").splitlines()
    rows = list(csv.DictReader(text))

`splitlines()` 先把文件切成行，而 CSV **允许引号字段里带换行**。
一条记录被看成两条之后，`DictReader` 从那一列起全部错位：

    p-000959,2015,demand_price,1,钢材下游需求…,"（风险章节「产品价格风险」p18）鉴于钢铁行业产能过剩、
    同质化竞争严重…",…,pending,,
                    ↑ 这里被切开，后面每一列都串了位

表现出来的样子有两种，**都不是报错**：

  · `conclusion` 读成空串 → 校验报「取值 '' 不合法」，看着像 CSV 写坏了；
  · 更糟的是它**恰好**还能通过校验——那时半句 evidence 会被当成
    `mitigation` 写回库，而它看起来就是一条填得整整齐齐的记录。

R 的证据片段是从 PDF 正文里剪的，**天然带硬换行**，所以这条一定会撞上。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

#: 仓库根。脚本在 `<repo>/scripts/`，不在 `backend/scripts/`。
ROOT = Path(__file__).resolve().parents[4]


def _load_script():
    """按路径加载 `scripts/export_input_templates.py`（同 `test_rekey.py`）。"""
    path = ROOT / "scripts" / "export_input_templates.py"
    spec = importlib.util.spec_from_file_location("_eit_multiline", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_eit_multiline"] = mod
    spec.loader.exec_module(mod)
    return mod


eit = _load_script()

HEADER = "project_id,period,item,conclusion,evidence"
NOTE = "↑ 项目编号,↑ 年度,↑ 四项之一,↑ 结论," + "↑ 证据"


@pytest.fixture()
def csv_with_newline(tmp_path: Path) -> Path:
    p = tmp_path / "R_风险检查.csv"
    p.write_text(
        HEADER + "\n"
        + NOTE + "\n"
        + 'p-000959,2015,demand_price,pending,"第一行\n第二行"\n',
        encoding="utf-8",
    )
    return p


def test_a_quoted_newline_stays_inside_its_column(csv_with_newline):
    """引号里的换行是**那一格的一部分**，不该把记录切断。"""
    _, rows = eit._read(csv_with_newline)
    assert len(rows) == 1
    assert rows[0]["conclusion"] == "pending"
    assert rows[0]["item"] == "demand_price"
    assert rows[0]["evidence"] == "第一行\n第二行"


def test_the_columns_after_it_are_not_shifted(csv_with_newline):
    """错位的判据：后面每一列都必须还在它自己的位置上。

    ⚠ 只断言「读出来 2 行」是不够的——错位的读法也会给出 `evidence`
    的一半内容，看着像读到了。要**逐列**核对。
    """
    _, rows = eit._read(csv_with_newline)
    assert set(rows[0]) == {"project_id", "period", "item", "conclusion", "evidence"}
    assert rows[0]["project_id"] == "p-000959"
    assert rows[0]["period"] == "2015"


def test_the_explanation_row_is_still_skipped(csv_with_newline):
    """第二行那个 `↑` 说明行照旧要丢掉。"""
    _, rows = eit._read(csv_with_newline)
    assert all(not v.startswith("↑") for v in rows[0].values())
