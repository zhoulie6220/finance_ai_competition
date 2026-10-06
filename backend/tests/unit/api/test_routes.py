"""REST 路由的测试。

重点盯三件事：

1. **金额必须是字符串。** 一旦变成 JSON number 就经过 float，几十亿的金额
   会掉精度（12345678901.23 → 12345678901.229998），而这是静默的。
2. **可比公司的网格不能是空的。** 旧视图硬编码 is_primary=1，会让华菱、首钢
   每格都是 not_found 且不报错——页面上看起来像「年报没披露」。
3. **空库要有话说。** 库能打开但是空的，所有页面显示空白而没有任何提示。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.routes import get_con
from app.db import repository
from app.db.session import connect, init_schema, load_seeds
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[3]
REAL_DB = BACKEND_DIR / "var" / "finance.db"
REAL_DB_AVAILABLE = REAL_DB.exists() and REAL_DB.stat().st_size > 1_000_000


def _override(db_path: Path):
    def dep() -> Iterator[sqlite3.Connection]:
        con = connect(db_path)
        try:
            yield con
        finally:
            con.close()

    return dep


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    """建一个只有 schema+种子的小库，塞一个项目与两笔事实。"""
    db = tmp_path / "api.db"
    con = connect(db)
    init_schema(con)
    load_seeds(con)
    con.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code, industry,"
        " fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
        ("p1", "测试", "某钢铁", "600019.SH", "steel", '["2023","2024"]',
         "2026-09-26T00:00:00"),
    )
    con.execute(
        "INSERT INTO file (file_id, project_id, role, period, rel_path, sha256,"
        " bytes, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        ("f1", "p1", "annual_report", "2024", "samples/x.pdf", "abc", 10,
         "2026-09-26T00:00:00"),
    )
    con.execute(
        "INSERT INTO document_page (page_id, file_id, page_no, text, text_source)"
        " VALUES (?,?,?,?,?)",
        ("pg1", "f1", 12, "应付账款 21,385,905,275.51", "native"),
    )
    con.execute(
        "INSERT INTO financial_fact (fact_id, project_id, company_id, is_primary,"
        " metric_key, value_millions, unit, period, period_kind, scope, source_file,"
        " source_file_id, source_page, source_text, confidence, status, extractor,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("fx1", "p1", "c1", 1, "accounts_payable", "21385.905276", "百万元", "2024",
         "instant", "consolidated", "samples/x.pdf", "f1", 12,
         "应付账款 21,385,905,275.51", 1.0, "validated", "rule:v3",
         "2026-09-26T00:00:00"),
    )
    con.commit()
    con.close()
    return db


@pytest.fixture()
def client(seeded_db: Path) -> Iterator[TestClient]:
    app.dependency_overrides[get_con] = _override(seeded_db)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def empty_client(tmp_path: Path) -> Iterator[TestClient]:
    """只有 schema 与种子、没有任何数据的库。

    这正是「init_db --force 跑过但没跑 merge_data_pack」的状态——
    库能打开，但每个页面都是空的。
    """
    db = tmp_path / "empty.db"
    con = connect(db)
    init_schema(con)
    load_seeds(con)
    con.commit()
    con.close()
    app.dependency_overrides[get_con] = _override(db)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------- 健康检查


def test_health_reports_empty_reason(empty_client: TestClient):
    """★ 空库要说话。

    库能打开但是空的，所有页面显示空白，而没有任何地方提示原因——
    这是「静默」最典型的一种：没有报错、没有异常、只是什么都是空的。
    """
    body = empty_client.get("/api/health").json()
    assert body["status"] == "empty"
    assert body["db_ok"] is False
    # ⚠ 断言的是**本意**（空库要给一句人话、且说明是「没有数据」），
    #   不是某一句具体文案。原来钉的是「导回数据包」——
    #   那是给开发看的命令行，出现在页面上不合适，文案改过一次它就挂了。
    assert body["db_hint"], "空库必须给一句人话，不能什么都不说"
    assert "没有数据" in body["db_hint"]


def test_health_is_ok_when_data_is_present(client: TestClient):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert body["db_hint"] is None


def test_health_reports_llm_mode(client: TestClient):
    """llm_mode 驱动前端的离线回放横幅——回放时装作实时是诚信问题。"""
    body = client.get("/api/health").json()
    assert body["llm_mode"] in ("live", "replay")
    assert body["llm_description"]


# ---------------------------------------------------------------- 金额精度


def test_money_is_serialized_as_a_string(client: TestClient):
    """★ 金额必须是字符串。

    变成 JSON number 就经过 float，几十亿的金额会掉精度且不报错。
    """
    grid = client.get("/api/projects/p1/fact-grid").json()
    cell = grid["metrics"][0]["cells"]["2024"]
    assert isinstance(cell["value"], str)
    assert cell["value"] == "21385.905276"

    fact = client.get("/api/facts/fx1").json()
    assert isinstance(fact["value"], str)
    assert fact["value"] == "21385.905276"


# ---------------------------------------------------------------- 网格


def test_grid_shape_is_metric_by_year(client: TestClient):
    """网格是完整的指标 × 年度笛卡尔积，没数据的格子显示 not_found。

    ★ 必须返回**全部**指标，不只是有数据的那些。少返回会让「没找到」
    和「这个指标不存在」在页面上长得一模一样，而且不报错。

    ⚠ 也正因如此别按下标取——要按键查。这一条是被自己的测试绊了一次
    才写下来的。
    """
    grid = client.get("/api/projects/p1/fact-grid").json()
    assert grid["periods"] == ["2023", "2024"]

    by_key = {m["metric_key"]: m for m in grid["metrics"]}

    # 一个肯定没有任何事实的指标也必须出现（审计意见是文本披露项）。
    # 它缺席就说明网格只返回了「有数据的指标」——多半是又加回了 scope 过滤。
    assert "audit_opinion" in by_key, "网格少了没有数据的指标"
    assert all(
        c["status"] == "not_found" for c in by_key["audit_opinion"]["cells"].values()
    )
    assert len(by_key) > 50, "指标数明显偏少"

    metric = by_key["accounts_payable"]
    assert metric["cells"]["2024"]["value"] == "21385.905276"
    # 没数据的年份仍然占一格，状态是 not_found 而不是缺行——
    # 「缺失即无行」在展示层表现为「未找到」，**不是 0**
    assert metric["cells"]["2023"]["status"] == "not_found"
    assert metric["cells"]["2023"]["value"] is None


def test_unknown_project_is_404_with_the_id(client: TestClient):
    r = client.get("/api/projects/no-such-project")
    assert r.status_code == 404
    assert "no-such-project" in r.json()["detail"]


# ---------------------------------------------------------------- 证据链


def test_fact_jumps_back_to_the_original_page(client: TestClient):
    """★ 证据链的终点：从一笔事实回到年报原文与页码。"""
    page = client.get("/api/facts/fx1/page").json()
    assert page["page_no"] == 12
    assert "应付账款" in page["text"]
    assert page["file_name"] == "x.pdf"      # 只给文件名，不外泄绝对路径


def test_missing_page_gives_an_actionable_message(client: TestClient):
    """定位不到时必须说「多半是哪里对不上」，而不是只说 404。"""
    r = client.get("/api/pages/no-such-page")
    assert r.status_code == 404
    assert "no-such-page" in r.json()["detail"]


# ---------------------------------------------------------------- 勾稽


def test_checks_endpoint_runs_and_reports_coverage(client: TestClient):
    body = client.get("/api/checks", params={"project_id": "p1"}).json()
    s = body["summary"]
    # 覆盖率与不平数必须一起给，只给一个会被误读
    assert s["total"] == s["evaluable"] + s["skipped_missing_data"] + s["skipped_incomparable"]
    assert "可评估" in s["coverage_line"]
    assert "sheet_ok" in s


def test_checks_persist_and_read_back(client: TestClient):
    client.get("/api/checks", params={"project_id": "p1"})
    stored = client.get("/api/checks/stored", params={"project_id": "p1"}).json()
    assert len(stored["results"]) > 0
    assert all(r["project_id"] if "project_id" in r else True for r in stored["results"])


def test_checks_without_persist_leaves_the_table_alone(client: TestClient):
    """persist=0 用于「改完规则参数先看看会怎样」，不该留下痕迹。"""
    client.get("/api/checks", params={"project_id": "p1", "persist": False})
    stored = client.get("/api/checks/stored", params={"project_id": "p1"}).json()
    assert stored["results"] == []


# ---------------------------------------------------------------- 真实库上的回归


@pytest.mark.skipif(not REAL_DB_AVAILABLE, reason="需要已导入数据包的真实库")
def test_real_comparable_projects_have_grid_values():
    """★ 可比公司的网格不能是空的。

    旧视图 v_fact_grid 硬编码 is_primary=1，华菱、首钢的事实全是 0，
    于是网格每格都是 not_found——页面上看起来像「年报没披露」，且不报任何错。

    这条跑在真实库上，因为小库造不出这个场景（小库里只有一家公司）。
    """
    app.dependency_overrides[get_con] = _override(REAL_DB)
    try:
        c = TestClient(app)
        for pid in ("p-000932", "p-000959"):
            grid = c.get(f"/api/projects/{pid}/fact-grid").json()
            filled = sum(
                1
                for m in grid["metrics"]
                for cell in m["cells"].values()
                if cell["status"] != "not_found"
            )
            assert filled > 0, f"{pid} 的网格全是空的——多半是又换回 v_fact_grid 了"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------- 响应模型不许丢字段
#
# ⚠ 这一组盯的是 `response_model` **反方向**的坑。
#
# 少了必填字段时接口会 500，很吵，一眼能看见——那是好事。
# 真正的坑是**多出来的字段被静默删掉**：路由返回的 dict 里有 `files`，
# 而响应模型里没写，FastAPI 就按模型裁剪，前端拿到 `undefined`，
# 不报错、不告警，只是那个字段永远显示不出来。
#
# 2026-09-30 给 13 个工作台接口补响应模型时就踩到了这一条：
# `GET /api/projects/{id}` 的 `files` 被裁掉了。所以在这里逐条对账，
# 不是靠「接口没报错」当作通过。


def _keys(x: object) -> set[str]:
    if isinstance(x, dict):
        return set(x)
    if isinstance(x, list) and x and isinstance(x[0], dict):
        return set(x[0])
    return set()


def test_project_list_response_keeps_every_repository_field(
    client: TestClient, seeded_db: Path
) -> None:
    """列表接口：仓储层给几列，接口就要出几列。"""
    con = connect(seeded_db)
    try:
        raw = _keys(repository.list_projects(con))
    finally:
        con.close()
    api = _keys(client.get("/api/projects").json())
    assert raw <= api, f"这些字段被响应模型裁掉了：{sorted(raw - api)}"


def test_project_detail_response_keeps_every_repository_field(
    client: TestClient, seeded_db: Path
) -> None:
    """详情接口。

    ★ 这一条是**回归测试**：`files` 曾经就是在这里被裁掉的——
    仓储层返回了它，`ProjectCard` 里没写，于是项目详情页的文件列表
    永远是空的，而且不报错。
    """
    con = connect(seeded_db)
    try:
        raw = _keys(repository.get_project(con, "p1"))
    finally:
        con.close()
    api = _keys(client.get("/api/projects/p1").json())
    assert raw <= api, f"这些字段被响应模型裁掉了：{sorted(raw - api)}"


def test_fact_detail_response_keeps_every_repository_field(
    client: TestClient, seeded_db: Path
) -> None:
    """事实详情是字段最多的一个（30 列），最容易被裁。"""
    con = connect(seeded_db)
    try:
        raw = _keys(repository.get_fact(con, "fx1"))
    finally:
        con.close()
    assert raw, "夹具里的 fx1 没取到，测试本身失效了"
    api = _keys(client.get("/api/facts/fx1").json())
    assert raw <= api, f"这些字段被响应模型裁掉了：{sorted(raw - api)}"


def test_rule_config_response_keeps_every_repository_field(client: TestClient) -> None:
    """口径参数：页面要按 `default_value` 做「恢复默认」，裁掉就没法恢复。"""
    page = client.get("/api/rule-config").json()
    assert page, "种子里的 rule_config 是空的"
    assert {"key", "value", "default_value"} <= set(page[0]), (
        f"口径参数少了字段：{sorted(page[0])}"
    )
