"""任务编排、工具调用与日志。

赛事要求「完整记录文件访问、工具调用、计算过程和结果生成情况，确保数据来源可核验、
执行过程可追溯、运行结果可复现」。本模块是那三项的落点：

    ToolCall      每一次工具调用的入参、出参摘要与耗时
    LlmCall       每一次模型调用的 prompt 版本、token、原始输出（可回放）
    FileAccessLog 每一次文件访问的白名单校验结果
    RunManifest   复现一次的完整凭据：代码版本、依赖锁哈希、prompt 哈希、规则版本、种子
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import LogLevel, StepStatus, TaskStatus, ToolTransport


class TaskStep(BaseModel):
    """任务计划中的一步。"""

    model_config = ConfigDict(from_attributes=True)

    step_id: str
    task_id: str
    seq: int
    name: str
    skill_key: str | None = None
    tool_name: str | None = None
    status: StepStatus
    depends_on: list[str] = Field(
        default_factory=list, description="依赖的 step_id，供前端画时间线"
    )
    input_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None
    retry_of: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class Task(BaseModel):
    """一次编排任务。

    Agent 必须**先生成计划再执行**（plan 非空且 plan_confirmed），不允许边想边做——
    否则「执行过程可追溯」无从谈起。
    """

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    project_id: str | None = None
    user_input: str = Field(description="用户原话")
    intent: str | None = None
    skill_key: str | None = None
    plan: dict | None = Field(default=None, description="步骤 + 依赖 + 工具 + 是否含 LLM")
    plan_confirmed: bool = False
    status: TaskStatus
    llm_plan_call_id: str | None = None
    error: str | None = None
    manifest_id: str | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class ToolCall(BaseModel):
    """一次工具调用。"""

    model_config = ConfigDict(from_attributes=True)

    call_id: str
    task_id: str | None = None
    step_id: str | None = None
    tool_name: str
    tool_version: str
    transport: ToolTransport
    deterministic: bool = Field(
        description="True=纯计算由程序完成；False=含 LLM。前端据此区分"
                    "「程序算的」与「模型理解的」，评审也能一眼看出边界"
    )
    args: dict = Field(default_factory=dict, description="入参（已脱敏）")
    result_summary: str
    result_hash: str | None = None
    status: str
    duration_ms: int | None = None
    error: str | None = None
    created_at: str


class LlmCall(BaseModel):
    """一次模型调用。

    `output` 保存原始输出，配合 prompt_hash + input_hash 可以离线回放——
    现场断网时走预录响应，**界面必须显著标注「离线回放模式」**。
    """

    model_config = ConfigDict(from_attributes=True)

    call_id: str
    task_id: str | None = None
    step_id: str | None = None
    purpose: str = Field(description="intent / plan / claim_extract / memo_text")
    model: str
    model_version: str | None = None

    prompt_key: str
    prompt_version: str
    prompt_hash: str
    params: dict = Field(default_factory=dict, description="temperature/seed 等，**不含密钥**")
    input_hash: str
    input_digest: str | None = None

    output: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    cached: bool = False
    cassette_id: str | None = Field(default=None, description="离线回放的来源")
    status: str
    error: str | None = None
    created_at: str


class FileAccessLog(BaseModel):
    """一次文件访问。「完整记录文件访问」的落点。"""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    ts: str
    actor: str = Field(description="'user:<uid>' / 'tool:<name>' / 'mcp:<server>'")
    file_id: str | None = None
    rel_path: str = Field(description="相对 DATA_ROOT 的路径；绝对路径不入库")
    action: str = Field(description="read / open / preview / export / deny")
    page_no: int | None = None
    task_id: str | None = None
    tool_call_id: str | None = None
    sha256_verified: bool | None = None
    allowed: bool
    deny_reason: str | None = None


class RunManifest(BaseModel):
    """复现一次的完整凭据。

    「同一份输入和配置可以重新生成同一计算结果」不是靠承诺，是靠这张表：
    代码版本 + 依赖锁哈希 + 输入文件 sha256 + 模型名版本 + prompt 注册表哈希 +
    规则版本 + 随机种子，缺一不可。
    """

    model_config = ConfigDict(from_attributes=True)

    manifest_id: str
    project_id: str | None = None
    task_id: str | None = None
    code_version: str
    code_dirty: bool = Field(description="工作区有未提交改动时为 True，提示结果可能不可复现")
    python_version: str
    deps_lock_hash: str
    sample_pack_version: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    prompt_registry_hash: str
    rule_config_version: int
    random_seed: int | None = None
    input_file_hashes: dict[str, str] = Field(
        default_factory=dict, description="{file_id: sha256}"
    )
    output_hash: str | None = None
    offline_replay: bool = False
    created_at: str


class AppLog(BaseModel):
    """结构化应用日志。

    主存数据库而非文件：`.gitignore` 忽略了 `logs`，写文件的日志不会随项目导出，
    也无法在页面上按 task_id 查询。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    ts: str
    level: LogLevel
    logger: str
    event: str
    request_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    message: str
    payload: dict | None = None
    duration_ms: int | None = None


class TaskEvent(BaseModel):
    """SSE 事件。前端按 seq 游标重连，断线时带 Last-Event-ID 续传，保证时间线不丢帧。"""

    seq: int
    ts: str
    task_id: str
    step_id: str | None = None
    type: str = Field(
        description="plan.created / step.started / step.succeeded / step.failed / "
                    "tool.called / tool.result / llm.called / llm.result / progress / "
                    "warning / evidence.attached / review.required / task.completed / heartbeat"
    )
    payload: dict = Field(default_factory=dict)
