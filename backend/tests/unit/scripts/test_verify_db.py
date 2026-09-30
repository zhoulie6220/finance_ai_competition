"""verify_db.py 的**反例**测试。

只断言「真实数据能通过」是不够的：一个写坏了的检查函数对着空库也会安安静静
地返回 0，而「规则没生效」和「数据恰好干净」看起来一模一样。这个脚本又是
R1（init_db --force 清空不可再生数据）的唯一防线，它要是悄悄不失败，
整批数据没了都没人知道。

所以这里刻意构造**必须被判失败**的库，断言它确实失败。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# tests/unit/scripts/test_verify_db.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parents[3]


def _load_verify_db() -> ModuleType:
    """按文件路径加载脚本。scripts/ 不是包，不能直接 import。"""
    spec = importlib.util.spec_from_file_location(
        "verify_db", BACKEND_DIR / "scripts" / "verify_db.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(module: ModuleType, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["verify_db.py", *argv])
    return module.main()


@pytest.fixture()
def empty_db(tmp_path: Path) -> Path:
    """只有 schema 与种子、一条数据包内容都没有的库。

    这正是「init_db.py --force 跑过但没跑 merge_data_pack.py」那个状态。
    """
    from app.db.session import connect, init_schema, load_seeds

    path = tmp_path / "empty.db"
    con = connect(path)
    try:
        init_schema(con)
        load_seeds(con)
        con.commit()
    finally:
        con.close()
    return path


def test_empty_database_is_rejected(empty_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空库必须被判失败。这是本脚本存在的全部理由。"""
    module = _load_verify_db()
    assert _run_main(module, monkeypatch, "--db", str(empty_db)) == 1


def test_missing_database_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库文件不存在也必须非零退出，不能当作「没什么可查的」放过去。"""
    module = _load_verify_db()
    assert _run_main(module, monkeypatch, "--db", str(tmp_path / "nope.db")) == 1


def test_expected_counts_are_all_positive() -> None:
    """期望值写错成 0 会让检查恒真——空库也能通过。"""
    module = _load_verify_db()
    for label, _sql, expected in (
        *module.DATA_CHECKS,
        *module.VIEW_CHECKS,
        *module.SEED_CHECKS,
    ):
        assert expected > 0, f"{label} 的期望值是 {expected}，恒真检查拦不住任何东西"


def test_data_checks_cover_the_irreplaceable_tables() -> None:
    """数据包里有、且没有任何脚本能重新生成的表，一条都不能漏检。"""
    module = _load_verify_db()
    checked = {label for label, _sql, _n in module.DATA_CHECKS}
    assert {
        "project",
        "file",
        "document_page",
        "page_fts",
        "financial_fact",
        "fact_observation",
        "mdna_section",
    } <= checked
