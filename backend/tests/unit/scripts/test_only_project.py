"""`--project`：发出去只有一家的表时，收回来也只能导那一家。

★ 这条护栏挡的是一个**会把已经出分的项目踢出分**的坑。

三张表平时是三家混在一个 CSV 里的，而 `--import` 会把**每一行**都写进去
（包括还是 `pending` 的新行）。宝钢、华菱的候选集里各有 1 条新主张还没人判，
导进去 P 就不完整 —— **两家的分同时没了，而且不报错**。

2026-10-09 实测撞过。当时的金标准做法是手工把 CSV 换成只含一家的再导，
但那**靠人记得还原**：漏一步就是两家的分没了。所以做成开关。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.db.session import connect_memory, init_schema

#: 仓库根。脚本在 `<repo>/scripts/`，不在 `backend/scripts/`。
ROOT = Path(__file__).resolve().parents[4]


def _load_script():
    path = ROOT / "scripts" / "export_input_templates.py"
    spec = importlib.util.spec_from_file_location("_eit_project", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_eit_project"] = mod
    spec.loader.exec_module(mod)
    return mod


eit = _load_script()


@pytest.fixture()
def con():
    c = connect_memory()
    init_schema(c)
    yield c
    c.close()


@pytest.fixture()
def csv_dir(tmp_path, monkeypatch):
    """把 OUT_DIR 指到临时目录，按真实格式写三份 CSV。"""
    monkeypatch.setattr(eit, "OUT_DIR", tmp_path)
    head = "project_id,period,scope,receivable_gross,over_one_year,revenue," \
           "source_page,source_text,status,note,reviewer"
    note = ",".join(f"↑ {d}" for _, d in eit.Q2_COLUMNS)
    (tmp_path / "Q2_账龄.csv").write_text(
        head + "\n" + note + "\n"
        + "p-a,2020,consolidated,1,1,1,1,原文,validated,,赵\n"
        # ⚠ 这一行的 status 是**自造值**——不带过滤时应当被 `--check` 抓出来
        + "p-b,2020,consolidated,1,1,1,1,原文,自造的假状态,,钱\n",
        encoding="utf-8",
    )
    # R 与 P 给空表（只要文件存在，校验器就不会报「文件不存在」）
    for name, cols in (("R_风险检查.csv", eit.R_COLUMNS),
                       ("P_人工确认.csv", eit.P_COLUMNS)):
        (tmp_path / name).write_text(
            ",".join(c for c, _ in cols) + "\n"
            + ",".join(f"↑ {d}" for _, d in cols) + "\n",
            encoding="utf-8",
        )
    return tmp_path


def test_without_the_flag_a_bad_row_in_another_project_blocks_everything(con, csv_dir):
    """不带 `--project`：别的项目里那一行**不合法**，整批被拒。"""
    assert eit._check_or_import(con, do_import=False) == 1


def test_with_the_flag_other_projects_are_not_even_looked_at(con, csv_dir):
    """带 `--project p-a`：p-b 那一行**根本没进校验**，所以能过。"""
    assert eit._check_or_import(con, do_import=False, only_project="p-a") == 0


def test_write_filters_rows_by_project(tmp_path):
    """导出侧同样过滤——发出去的 CSV 里只有一家的行。"""
    rows = [{"project_id": "p-a", "x": "1"}, {"project_id": "p-b", "x": "2"}]
    out = tmp_path / "t.csv"
    eit._write(out, (("project_id", "说明"), ("x", "说明")), rows,
               only_project="p-a")
    text = out.read_text(encoding="utf-8-sig")
    assert "p-a" in text
    assert "p-b" not in text
