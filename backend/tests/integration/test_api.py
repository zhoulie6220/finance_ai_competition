"""HTTP 层的端到端测试。

跑的是**完整的应用**：真实路由、真实数据库（临时目录里的一份）、真实事件总线。
不 mock 任何东西——这一层的价值恰恰在于验证各层接得上：
如果这里也要 mock，那它证明的就是 mock 的行为，不是系统的行为。

覆盖四件演示时必须成立的事：
  1. 一句话进来 → 任务时间线 → 出结果（整条链路）
  2. SSE 能订阅、能续传
  3. 工具调用都留了痕
  4. 断线重连带上 Last-Event-ID 不丢帧
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.session import connect, init_schema, load_seeds


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把整个应用指到临时数据库上。

    靠的是 `DATA_ROOT` 环境变量 + 清空 `get_settings` 的缓存——
    这也是 `connect()` 为什么必须读 settings 而不是读模块级常量：
    写死常量的话，改 `DATA_ROOT` 就只会影响文件读写、不影响数据库。
    """
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    con = connect()
    init_schema(con)
    load_seeds(con)
    con.commit()
    con.close()

    # 应用实例必须在配置改完之后再建，否则 lifespan 里拿的是旧配置
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def post_task(client, text: str, **extra) -> dict:
    body = {"input": text, "sync": True, **extra}
    r = client.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _drain(client, task_id: str, last_event_id: int | None) -> str:
    """读完整条事件流。

    `last_event_id=None` 表示**不带 lastEventId 参数**——那是 EventSource
    首次连接的样子，与「带个 0」是两条不同的代码路径。
    """
    query = "" if last_event_id is None else f"?lastEventId={last_event_id}"
    with client.stream("GET", f"/api/tasks/{task_id}/events{query}") as r:
        return "".join(chunk for chunk in r.iter_text())


def read_stream(fn, timeout: float = 10.0):
    """读一个 SSE 流，**带超时**。

    任务到终态后服务端必须自己关流。不关的话连接会一直挂着，
    浏览器那边显示「重连中」——一个已经跑完的任务看起来像卡住了。

    超时后用 `pytest.fail` 而不是让线程继续挂着：这里挂了意味着流没关，
    而那是一个**断言失败**，不是「测试环境慢」。让整个测试套件卡死
    会掩盖真正的原因，下一个跑测试的人只会以为 CI 抽风了。
    """
    import threading

    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        pytest.fail(
            f"SSE 流在 {timeout}s 内没有结束。任务已到终态时流必须自动关闭"
            "（见 app/api/events.py 的 TASK_ENDING_EVENTS）"
        )
    if "error" in box:
        raise box["error"]
    return box["value"]


# ---------------------------------------------------------------- 健康与元信息


def test_health_reports_real_counts(client) -> None:
    d = client.get("/health").json()
    assert d["status"] == "ok"
    assert d["database"]["ok"] is True
    # 断言的是「种子数据真的装载了」，不是某个具体数字——
    # 数错一个字段就变红的测试会被人随手改掉
    assert d["database"]["counts"]["metric_definition"] > 80
    assert d["database"]["counts"]["rule_config"] > 50


def test_tools_endpoint_exposes_json_schema(client) -> None:
    """Tool 清单要能直接回答「你们的 Tool 模块长什么样」。

    必须带 JSON Schema —— 赛事要求 Tool 是可被外部（MCP）调用的定义，
    只列个名字说明不了这一点。
    """
    d = client.get("/api/tools").json()
    assert d["count"] >= 3
    for t in d["tools"]:
        assert t["input_schema"]["type"] == "object"
        assert t["version"], "工具必须声明版本：结果哈希依赖它"
    assert d["mcp_preview"], "MCP 预览不能为空"


def test_skills_endpoint_lists_registered_skills(client) -> None:
    d = client.get("/api/skills").json()
    assert d["count"] >= 1
    assert any(s["key"] == "selfcheck" for s in d["skills"])


# ---------------------------------------------------------------- 整条链路


def test_task_runs_end_to_end(client) -> None:
    """一句话 → 计划 → 逐步执行 → 结构化结果。

    这是计划里「第 1 周用假数据真链路打穿一条端到端垂直线」那条要求。
    """
    d = post_task(client, "跑一次系统自检")
    assert d["task"]["status"] == "succeeded"
    assert len(d["steps"]) == 4
    assert all(s["status"] == "succeeded" for s in d["steps"])
    assert d["steps"][-1]["output"], "最后一步没有产出摘要"


def test_task_events_are_queryable_after_the_fact(client) -> None:
    """页面刷新后要先能拉到已发生的事件，否则等待期间是空白。"""
    d = post_task(client, "跑一次系统自检")
    task_id = d["task"]["task_id"]

    h = client.get(f"/api/tasks/{task_id}/events/history").json()
    assert h["count"] >= 10
    types = [e["type"] for e in h["events"]]
    assert types[0] == "plan.created"
    assert "task.completed" in types
    # seq 从 1 开始且连续，前端靠它判断有没有缺格
    assert [e["seq"] for e in h["events"]] == list(range(1, h["count"] + 1))


def test_sse_stream_delivers_frames(client) -> None:
    """SSE 真的能推事件，且每帧带 id 与 event。

    用 `stream()` 逐块读而不是 `get()` 整个响应：SSE 是流式的，
    一次性读完会掩盖「响应头出去了但事件一直没来」这类问题。
    """
    d = post_task(client, "跑一次系统自检")
    task_id = d["task"]["task_id"]

    def read() -> tuple[int, dict, str]:
        with client.stream("GET", f"/api/tasks/{task_id}/events?lastEventId=0") as r:
            return (
                r.status_code,
                dict(r.headers),
                "".join(chunk for chunk in r.iter_text()),
            )

    status, headers, body = read_stream(read)
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")

    # 订阅时任务已经跑完了，所以走的是「补发历史」这条路径
    assert "event: task.completed" in body
    assert "id: 1\n" in body
    assert 'data: {"seq":' in body


def test_sse_resumes_without_gaps(client) -> None:
    """带 Last-Event-ID 订阅时从下一条开始补发，不重不漏。

    这是「时间线不丢帧」的落点：一次网络抖动不该让演示现场少一格，
    也不该让前端把同一帧处理两遍（那会重复插入两个步骤）。
    """
    d = post_task(client, "跑一次系统自检")
    task_id = d["task"]["task_id"]
    total = client.get(f"/api/tasks/{task_id}/events/history").json()["count"]

    body = read_stream(lambda: _drain(client, task_id, last_event_id=4))
    seqs = [
        int(line.split(": ", 1)[1])
        for line in body.splitlines()
        if line.startswith("id: ")
    ]
    assert seqs == list(range(5, total + 1))


def test_tool_calls_are_fully_recorded(client) -> None:
    """「工具调用情况完整记录」的接口级验证。

    刻意断言 `deterministic` 字段：前端据此把「程序算的」与「模型理解的」
    分开呈现，评审也靠它一眼看出边界。缺了它，这个区分就没了。
    """
    d = post_task(client, "跑一次系统自检")
    task_id = d["task"]["task_id"]

    c = client.get(f"/api/tasks/{task_id}/tool-calls").json()
    assert c["count"] == 3
    for call in c["calls"]:
        assert call["status"] == "succeeded"
        assert call["result_hash"], "缺结果哈希就无从做复现比对"
        assert call["tool_version"]
        assert call["deterministic"] in (0, 1)
        assert call["duration_ms"] is not None


# ---------------------------------------------------------------- 失败路径


def test_unroutable_request_is_rejected_not_guessed(client) -> None:
    """判不出该用哪个 Skill 时要报错，不能随便挑一个跑。

    挑错了会跑完一遍、出一份和问题无关的结果——而用户以为那就是答案。
    """
    r = client.post("/api/tasks", json={"input": "帮我预测一下明年的钢价"})
    assert r.status_code == 400
    assert "Skill" in r.json()["detail"]


def test_empty_input_is_rejected(client) -> None:
    r = client.post("/api/tasks", json={"input": ""})
    assert r.status_code == 422  # Pydantic 的 min_length


def test_missing_task_returns_404(client) -> None:
    assert client.get("/api/tasks/t-不存在").status_code == 404


def test_nonexistent_project_is_400_not_500(client) -> None:
    """填了不存在的 project_id，要得到一句能看懂的 400，而不是裸 sqlite 报错的 500。

    这个用例来自一次真实翻车：/docs 的「Try it out」把 `project_id` 预填成
    字符串 "string"，点下去就是 500「FOREIGN KEY constraint failed」。
    500 会让人去查接口，而问题在这条请求的数据上——排查方向从一开始就是错的。
    """
    r = client.post("/api/tasks", json={"input": "跑一次系统自检", "project_id": "string"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "项目不存在" in detail and "string" in detail
    # 不能把 sqlite 的原文甩给用户
    assert "FOREIGN KEY" not in detail


def _is_stringy(spec: dict) -> bool:
    """字段是不是字符串型（含 `str | None` 展开成的 anyOf 形态）。"""
    if spec.get("type") == "string":
        return True
    return any(o.get("type") == "string" for o in spec.get("anyOf", []))


def test_string_fields_in_task_body_have_examples(client) -> None:
    """字符串字段必须自带 example，否则 /docs 会预填 "string"。

    只盯字符串型：布尔字段的 `default: true` 会被 Swagger 正常预填成 `true`，
    对它提 example 要求是把断言写宽了 —— 宽断言会逼着人给 `run` 编一个
    `"true"` 样的假例子，那反而是在制造新的错误示范。
    """
    props = client.get("/openapi.json").json()["components"]["schemas"]["CreateTaskRequest"]
    assert "properties" in props, props
    stringy = {n: s for n, s in props["properties"].items() if _is_stringy(s)}
    assert stringy, "一个字符串字段都没有？请求体结构变了，这条测试该重写而不是删掉"
    for name, spec in stringy.items():
        assert "examples" in spec, f"{name} 没有 example，/docs 会预填 'string'"


def test_valid_project_id_is_accepted(client) -> None:
    """反过来也要成立：真实存在的 project_id 不能被上面那道检查误伤。"""
    p = client.post("/api/projects", json={
        "name": "自检项目", "company_name": "宝钢股份",
        "stock_code": "600019.SH", "industry": "steel", "fiscal_years": ["2024"],
    }).json()
    r = client.post("/api/tasks", json={
        "input": "跑一次系统自检", "project_id": p["project_id"], "sync": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["task"]["project_id"] == p["project_id"]


def test_async_mode_returns_immediately(client) -> None:
    """默认异步：创建后立刻返回，进度走 SSE。"""
    r = client.post("/api/tasks", json={"input": "跑一次系统自检"})
    assert r.status_code == 200
    assert r.json()["task"]["status"] in ("pending", "planned", "running", "succeeded")


# ---------------------------------------------------------------- 项目与文件


def test_project_lifecycle(client, tmp_path) -> None:
    p = client.post("/api/projects", json={
        "name": "宝钢股份 2015-2024",
        "company_name": "宝钢股份",
        "stock_code": "600019.SH",
        "industry": "steel",
        "fiscal_years": ["2015", "2016", "2017", "2018", "2019", "2020",
                         "2021", "2022", "2023", "2024"],
    }).json()
    assert p["project_id"]
    assert client.get("/api/projects").json()["projects"][0]["stock_code"] == "600019.SH"


def test_register_file_computes_hash(client, tmp_path) -> None:
    p = client.post("/api/projects", json={
        "name": "x", "company_name": "y", "stock_code": "600019.SH",
    }).json()

    (tmp_path / "samples").mkdir()
    pdf = tmp_path / "samples" / "600019_2024.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    f = client.post(f"/api/projects/{p['project_id']}/files", json={
        "rel_path": "samples/600019_2024.pdf", "role": "annual_report", "period": "2024",
    }).json()
    assert len(f["sha256"]) == 64
    assert f["bytes"] == len(b"%PDF-1.4 fake")
    # 解析尚未实现，状态必须诚实地停在 pending
    assert f["parse_status"] == "pending"


def test_duplicate_file_is_rejected(client, tmp_path) -> None:
    """同一份文件登记两次要被拒。

    UNIQUE(project_id, sha256) 挡的是「同一份年报导入两遍」——
    那会让每个数字出现两次，而聚合时不会报错，只会把利润翻倍。
    """
    p = client.post("/api/projects", json={
        "name": "x", "company_name": "y", "stock_code": "600019.SH",
    }).json()
    (tmp_path / "a.pdf").write_bytes(b"same bytes")
    body = {"rel_path": "a.pdf", "role": "annual_report", "period": "2024"}

    assert client.post(f"/api/projects/{p['project_id']}/files", json=body).status_code == 200
    assert client.post(f"/api/projects/{p['project_id']}/files", json=body).status_code == 409


@pytest.mark.parametrize("bad", [
    "../secrets.pdf",            # 往上跳
    "/etc/passwd",               # 绝对路径
    "C:/Windows/win.ini",        # 带盘符
    "sub/../../out.pdf",         # 绕一圈再跳出去
])
def test_path_traversal_is_rejected(client, bad) -> None:
    """文件路径不得越出 DATA_ROOT。

    只靠 `Path.resolve()` 判断是不够的：Windows 上 `C:foo` 这类驱动器相对路径
    不会按预期解析，所以还要显式拒绝绝对路径与盘符。
    """
    p = client.post("/api/projects", json={
        "name": "x", "company_name": "y", "stock_code": "600019.SH",
    }).json()
    r = client.post(f"/api/projects/{p['project_id']}/files", json={
        "rel_path": bad, "role": "annual_report", "period": "2024",
    })
    assert r.status_code in (400, 404), f"{bad} 竟然通过了：{r.status_code}"


# ---------------------------------------------------------------- 重跑


def test_failed_task_can_be_retried_through_retrying(client, monkeypatch) -> None:
    """失败后重跑时，步骤必须经过 `retrying` 中转。

    直接从 failed 跳回 running 的话，「这一步重试过」在时间线上就消失了——
    而重试次数恰好是评审判断系统稳不稳的一个凭据。
    """
    from app.tools.registry import REGISTRY as TOOLS, ToolError

    real_call = TOOLS.call
    state = {"fail": True}

    async def flaky(spec, args, **kw):
        if state["fail"] and spec.name == "system.db_stats":
            raise ToolError("模拟首次失败")
        return await real_call(spec, args, **kw)

    monkeypatch.setattr(TOOLS, "call", flaky)

    d = post_task(client, "跑一次系统自检")
    task_id = d["task"]["task_id"]
    assert client.get(f"/api/tasks/{task_id}").json()["task"]["status"] == "failed"

    state["fail"] = False
    r = client.post(f"/api/tasks/{task_id}/retry")
    assert r.status_code == 200, r.text

    # 重跑是后台的，轮询到终态
    import time

    for _ in range(50):
        cur = client.get(f"/api/tasks/{task_id}").json()
        if cur["task"]["status"] == "succeeded":
            break
        time.sleep(0.1)

    events = client.get(f"/api/tasks/{task_id}/events/history").json()["events"]
    types = [e["type"] for e in events]
    assert "step.failed" in types
    # 关键断言：重试路径上出现过 retrying
    assert "step.retrying" in types, f"重试没有经过 retrying：{types}"


# ---------------------------------------------------------------- 中文化 / 响应契约


def test_validation_errors_are_chinese(client) -> None:
    """422 的文案是中文，且保留机读错误码。

    `msg` 是给人看的（会变），`type` 是给前端分支用的（不变）。
    两个都要有：只剩中文的话前端没法判断是「缺字段」还是「格式错」，
    只能去匹配文案——而文案迟早会改。
    """
    r = client.post("/api/tasks", json={"input": ""})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail, "422 应该有 detail 数组"
    for issue in detail:
        assert any("一" <= c <= "鿿" for c in issue["msg"]), f"报错不是中文：{issue}"
        assert issue["type"], "机读错误码不能丢"
    assert detail[0]["type"] == "string_too_short"


def test_missing_field_error_names_the_field(client) -> None:
    r = client.post("/api/projects", json={"name": "x", "stock_code": "600019.SH"})
    assert r.status_code == 422
    issue = next(i for i in r.json()["detail"] if i["type"] == "missing")
    assert issue["field"] == "company_name"
    assert "缺少必填字段" in issue["msg"]


def test_every_endpoint_declares_a_response_model(client) -> None:
    """每个接口的 200 都要指向一个真实模型。

    返回 `dict[str, Any]` 时 FastAPI 生成的是 `{"additionalProp1": {}}` 空壳——
    它看起来像一份文档，实际什么也没说，前端也没法据此生成类型。
    """
    spec = client.get("/openapi.json").json()
    untyped = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            content = op.get("responses", {}).get("200", {}).get("content", {})
            if "application/json" not in content:
                continue  # SSE 那条是 text/event-stream，见下一条测试
            schema = content["application/json"].get("schema", {})
            if "$ref" not in schema:
                untyped.append(f"{method.upper()} {path} → {schema}")
    assert not untyped, f"这些接口没声明响应模型：{untyped}"


def test_sse_endpoint_is_declared_as_an_event_stream(client) -> None:
    """事件流不能被显示成 JSON 接口——点开就会一直转圈。"""
    op = client.get("/openapi.json").json()["paths"]["/api/tasks/{task_id}/events"]["get"]
    assert "text/event-stream" in op["responses"]["200"]["content"]


def test_response_descriptions_are_chinese(client) -> None:
    """响应说明不能是 FastAPI 的英文默认值。"""
    spec = client.get("/openapi.json").json()
    english = {"Successful Response", "Validation Error"}
    found = [
        f"{m.upper()} {p} → {d}"
        for p, ops in spec["paths"].items()
        for m, op in ops.items()
        for d in (v.get("description") for v in op.get("responses", {}).values())
        if d in english
    ]
    assert not found, f"还是英文默认值：{found}"


def test_tool_call_args_is_an_object_not_a_string(client) -> None:
    """`args` 必须是对象。

    库里存的是 JSON 文本，仓储层不转的话接口返回一个字符串——前端写
    `call.args.include_llm` 得到 `undefined`，不报错，只是过滤条件静默失效。
    """
    d = post_task(client, "跑一次系统自检")
    calls = client.get(f"/api/tasks/{d['task']['task_id']}/tool-calls").json()["calls"]
    assert calls
    assert all(isinstance(c["args"], dict) for c in calls), [
        type(c["args"]).__name__ for c in calls
    ]


def test_project_json_columns_come_back_parsed(client) -> None:
    """`fiscal_years` 必须是数组，不是 JSON 文本。

    返回文本的话前端 `for (const y of years)` 会逐个**字符**迭代——
    不报错，只是年份变成 '2'、'0'、'2'、'4'。
    """
    r = client.post("/api/projects", json={
        "name": "宝钢", "company_name": "宝钢股份", "stock_code": "600019.SH",
        "industry": "steel", "fiscal_years": ["2023", "2024"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["fiscal_years"] == ["2023", "2024"]


def test_file_is_scanned_is_a_boolean(client, tmp_path) -> None:
    """`is_scanned` 必须是布尔，不是 0/1。

    JS 里字符串 '0' 是真值，返回 0/1 会让**每一份文件都被当成扫描件**。
    """
    import json as _json

    from app.config import get_settings

    root = get_settings().data_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    pdf = root / "t_is_scanned.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    try:
        p = client.post("/api/projects", json={
            "name": "x", "company_name": "x", "stock_code": "600019.SH",
        }).json()
        f = client.post(f"/api/projects/{p['project_id']}/files", json={
            "rel_path": pdf.name, "role": "annual_report", "period": "2024",
        }).json()
        assert f["is_scanned"] is False
        assert f["parse_status"] == "pending", "登记完不该说已解析"
        assert _json.dumps(f)  # 可序列化
    finally:
        pdf.unlink(missing_ok=True)


def test_fresh_subscriber_of_a_finished_task_gets_the_whole_timeline(client) -> None:
    """★ 全新订阅一个**已经跑完**的任务，必须拿到完整时间线。

    这是打开页面时最常见的路径：建任务 → 立刻订阅，而任务往往在订阅之前就跑完了。

    修之前这里返回的是**一条空流**：`last_event_id=None` 被当成「不发历史」，
    紧接着又因为任务已结束直接关流。任务成功了，时间线却一格都没有，
    页面上只看得到一个永远空着的框，而且**没有任何地方会报错**。
    """
    d = post_task(client, "跑一次系统自检")
    task_id = d["task"]["task_id"]
    expected = client.get(f"/api/tasks/{task_id}/events/history").json()["count"]

    # 不带 lastEventId —— 就是 EventSource 首次连接的样子
    body = read_stream(lambda: _drain(client, task_id, last_event_id=None))
    seqs = [int(l.split(": ", 1)[1]) for l in body.splitlines() if l.startswith("id: ")]

    assert seqs == list(range(1, expected + 1)), (
        f"全新订阅只拿到 {len(seqs)} 条，应该有 {expected} 条：{seqs}"
    )
    assert "event: task.completed" in body
