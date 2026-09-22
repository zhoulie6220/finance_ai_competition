"""HTTP 接口的响应契约。

**为什么要有这个文件。** 路由如果声明成 `-> dict[str, Any]`，FastAPI 就无法推断响应
结构，`/docs` 上会显示一个 `{"additionalProp1": {}}` 的空壳，前端也无法从 OpenAPI
生成类型。空壳示例比没有示例更糟：它看起来像一份文档，实际什么也没说。

这里定义的都是**接口形状**，不是业务实体——业务实体在各自的模块里
（`task.py` / `fact.py` / `valuation.py` …）。两者分开的理由是接口常要裁剪字段
（比如任务详情把 steps 单独拎出来），裁剪是一次性的展示决定，不该污染实体定义。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import FileRole, ParseStatus, ToolTransport
from app.schemas.task import Task, TaskEvent, TaskStep, ToolCall


# ------------------------------------------------------------------ 参数校验失败


class ValidationIssue(BaseModel):
    """一条参数校验失败。

    这个结构**刻意保持与 FastAPI 默认的 422 响应同形**（`detail` 数组，每项带
    `loc` / `type` / `msg`），只把 `msg` 换成中文。改形状会让前端里已有的
    `detail[0].msg` 取值方式失效，而那是所有人都见过的默认约定。
    """

    model_config = ConfigDict(title="参数校验问题")

    field: str = Field(description="点号分隔的字段路径，如 'project_id'")
    loc: list[str | int] = Field(description="Pydantic 的原始位置串")
    type: str = Field(description="机读错误码，如 'string_too_short'；前端按它分支")
    msg: str = Field(description="中文说明")
    input: Any | None = Field(default=None, description="触发问题的原始输入")


class ValidationErrorResponse(BaseModel):
    """请求参数没通过校验（HTTP 422）。"""

    model_config = ConfigDict(title="参数校验失败")

    detail: list[ValidationIssue]


# ------------------------------------------------------------------ 任务


class TaskStepView(BaseModel):
    """任务时间线上的一个步骤。

    是 `TaskStep` 的裁剪视图：只保留前端画时间线要用的字段，
    去掉 `input_ref` / `retry_of` 这类后端内部用的列。
    """

    model_config = ConfigDict(title="任务步骤（时间线视图）")

    step_id: str
    seq: int
    name: str
    tool: str | None = None
    status: str
    depends_on: list[str] = Field(default_factory=list)
    output: str | None = None
    error: str | None = None


class TaskSummary(Task):
    """任务本身，**不含步骤**。

    继承 `Task` 而不是另抄一份字段：抄一份的话，将来给 `Task` 加字段会漏掉这里，
    接口上就凭空少一个字段，而**没有任何地方会报错**。
    `exclude=True` 让序列化时丢掉 `steps`——步骤单独放在响应体的 `steps` 里。
    """

    model_config = ConfigDict(title="任务（不含步骤）")

    steps: list[TaskStep] = Field(default_factory=list, exclude=True)


class TaskResponse(BaseModel):
    """任务详情：任务本身 + 时间线步骤。"""

    model_config = ConfigDict(title="任务详情")

    task: TaskSummary
    steps: list[TaskStepView]


class TaskListResponse(BaseModel):
    """任务列表（不含步骤，避免列表接口驮上全部时间线）。"""

    model_config = ConfigDict(title="任务列表")

    tasks: list[TaskSummary]


class ToolCallListResponse(BaseModel):
    """一个任务的全部工具调用记录。"""

    model_config = ConfigDict(title="工具调用记录")

    count: int
    calls: list[ToolCall]


class EventHistoryResponse(BaseModel):
    """一次性返回已发出的 SSE 事件。

    给两个场景用：页面刷新后先拉历史再订阅实时流；以及排查「前端少了一格」时
    对照服务端到底发了什么。
    """

    model_config = ConfigDict(title="任务事件历史")

    count: int
    events: list[TaskEvent]


# ------------------------------------------------------------------ 元信息


class DatabaseStatus(BaseModel):
    model_config = ConfigDict(title="数据库状态")

    ok: bool
    path: str
    counts: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class HealthResponse(BaseModel):
    """健康检查。

    数据库不可用时返回的是 **503 + `status='degraded'`**，不是 200——
    一个永远返回 200 的健康检查等于没有健康检查。
    """

    model_config = ConfigDict(title="健康检查")

    status: str = Field(description="'ok' 或 'degraded'")
    database: DatabaseStatus
    offline_mode: bool = Field(description="离线回放模式；为真时界面必须显著标注")
    llm_configured: bool
    model: str = Field(description="使用的模型名，如 'deepseek-chat'")


class ToolSpecView(BaseModel):
    """一个已登记的 Tool。"""

    model_config = ConfigDict(title="工具定义")

    name: str
    version: str
    description: str
    deterministic: bool = Field(
        description="True=纯计算由程序完成；False=含 LLM。评审据此看边界"
    )
    side_effects: bool
    transport: ToolTransport
    input_schema: dict
    output_schema: dict


class ToolListResponse(BaseModel):
    model_config = ConfigDict(title="工具清单")

    count: int
    tools: list[ToolSpecView]
    mcp_preview: list[dict] = Field(
        default_factory=list, description="同一份 input_schema 经 MCP 暴露的样子"
    )


class SkillView(BaseModel):
    model_config = ConfigDict(title="Skill 定义")

    key: str
    name: str
    description: str


class SkillListResponse(BaseModel):
    model_config = ConfigDict(title="Skill 清单")

    count: int
    skills: list[SkillView]


class MetaResponse(BaseModel):
    """运行环境。密钥已脱敏（只显示是否配置,不显示值）。"""

    model_config = ConfigDict(title="运行环境")

    backend_dir: str
    data_root: str
    settings: dict


# ------------------------------------------------------------------ 项目与文件


class ProjectView(BaseModel):
    """一个投研项目（一家公司的若干年年报）。"""

    model_config = ConfigDict(title="项目")

    project_id: str
    name: str
    company_name: str
    stock_code: str = Field(description="如 '600019.SH'")
    industry: str = Field(description="'steel'；本次参赛只跑钢铁")
    base_currency: str
    fiscal_years: list[str] = Field(
        default_factory=list,
        description="覆盖的会计年度。库里是 JSON 文本，出库时转成数组",
    )
    base_scope: str = Field(description="默认会计口径：consolidated / parent")
    status: str = Field(description="'active' 或 'archived'")
    created_at: str


class ProjectListResponse(BaseModel):
    model_config = ConfigDict(title="项目列表")

    projects: list[ProjectView]


class FileView(BaseModel):
    """登记进项目的一份文件。

    `parse_status` 停在 'pending' 是**诚实的状态**：页面据此显示「待解析」，
    而不是把没解析过的文件显示成「已解析但没数据」。
    """

    model_config = ConfigDict(title="文件")

    file_id: str
    project_id: str
    role: FileRole
    period: str = Field(description="'2024' / '2024H1' / '2024Q3'")
    rel_path: str = Field(description="相对 DATA_ROOT；绝对路径禁止入库")
    sha256: str
    bytes: int
    page_count: int | None = None
    is_scanned: bool = Field(description="扫描件需走 OCR 通道，精度不同")
    parse_status: ParseStatus
    parse_error: str | None = None
    uploaded_at: str


class FileListResponse(BaseModel):
    model_config = ConfigDict(title="文件列表")

    files: list[FileView]
