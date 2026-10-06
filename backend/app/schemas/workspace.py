"""工作台接口的响应契约（`app/api/routes.py` 的 13 个接口）。

## 为什么单独一个文件，而不是并进 `api.py`

`api.py` 里是**任务编排**那套接口（`/api/tasks` + SSE、`/api/tools`、`/api/skills`、
`/api/meta`）的形状；这里的是**工作台读模型**（项目、事实网格、原文页、勾稽、
叙事与指数、口径参数）。

两者来自两条并行开发的分支，2026-09-30 合并时各自带着自己的接口层。合成一个文件
会让「这次改动动了哪一半」看不出来，而这两半的负责人和改动节奏都不同。分开之后
`git log` 一望即知。

## ⚠ 这些模型曾经不存在，代价是前端手抄了 345 行

原来这 13 个路由都声明成 `-> dict[str, Any]`（或 `list[dict[str, Any]]`），
FastAPI 生成的 OpenAPI 里就是一个 `{"additionalProp1": {}}` 空壳——看起来像文档，
实际什么也没说。前端于是只能手写 `frontend/src/api/types.ts` 来对齐，而手抄的
类型会在后端加字段时**静默过期**：后端加了字段、前端少写一行，没有任何地方报错，
只是那个字段永远显示不出来。

`frontend/src/types/contract.ts` 那套自动导出就是为了解决这件事，
这里补上模型之后它才覆盖得到工作台接口。

## ⚠ 命名带 `View` / `Card` 后缀不是洁癖

`schemas/` 下已经有 `Claim` / `ClaimMatch` / `CheckResult` / `ClaimType` 这些
**业务实体**，下面这些是它们的**接口投影**（裁剪过字段、金额转成字符串）。
同名会让 `from app.schemas import Claim` 拿到哪一个取决于 import 顺序，
而且不报错。

## ⚠ 金额与比率一律 `str`

见 `docs/01-data-contract.md`：JSON number 是 IEEE 754 双精度，几十亿的金额往返
会掉精度（12345678901.23 → 12345678901.229998）且不报错。前端只做格式化，
不做任何算术。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ------------------------------------------------------------------ 健康检查


class HealthCard(BaseModel):
    """工作台健康检查（`GET /api/health`）。

    `db_ok` 为假时 `db_hint` 必须给出去处——空库是能打开的，不提示的话
    刚重建完还没导数据的库会让所有页面显示空白，而没有任何地方说明原因。
    """

    model_config = ConfigDict(title="健康检查")

    status: str = Field(description="'ok' 或 'empty'")
    db_ok: bool
    db_hint: str | None = Field(default=None, description="库为空时的处置办法")
    counts: dict[str, int] = Field(description="各表行数，供页面显示「库里有东西」")
    llm_mode: str = Field(description="'live' 或 'replay'")
    llm_configured: bool
    llm_description: str
    offline_mode: bool = Field(description="离线回放时界面必须显著标注，不得假装实时")
    rule_config_version: int | None = None


# ------------------------------------------------------------------ 项目


class ProjectFileView(BaseModel):
    """项目下的一份文件（只出现在项目详情里，列表接口不带）。"""

    model_config = ConfigDict(title="项目文件")

    file_id: str
    role: str = Field(description="'annual_report' 等")
    period: str
    file_name: str
    page_count: int | None = None
    parse_status: str = Field(description="停在 'pending' 是诚实的状态")


class ProjectCard(BaseModel):
    """项目卡片（`GET /api/projects`、`GET /api/projects/{id}`）。

    比 `ProjectView` 多三个计数字段——工作台首页要显示「几份文件、几条事实、
    几段正文」，不带上这三个数就得到处再发一次请求。

    ⚠ `files` **只有详情接口（`GET /api/projects/{id}`）才带**，
    列表接口是空的。这一点是补这个模型才发现的：
    仓储层的 `get_project` 会多返回一个 `files` 键，而模型里没有——
    `response_model` 就把它**静默删掉**了，前端拿到 `undefined` 不报错，
    只是项目详情页的文件列表永远是空的。

    这正是 `response_model` 的取舍：**多写一个字段会 500（吵，但是对的），
    少写一个字段会静默丢数据**。所以加完模型一定拿真实响应对一遍字段集合，
    别只看接口没报错就算过。
    """

    model_config = ConfigDict(title="项目卡片")

    project_id: str
    name: str
    company_name: str
    stock_code: str
    industry: str
    base_scope: str
    fiscal_years: list[str] = Field(default_factory=list)
    file_count: int | None = None
    fact_count: int | None = None
    mdna_count: int | None = None
    files: list[ProjectFileView] | None = Field(
        default=None, description="仅详情接口返回；列表接口为 null"
    )


# ------------------------------------------------------------------ 财务事实网格


class FactCellView(BaseModel):
    """财务事实网格里的一格。

    **没有数据也有一格**，此时 `status='not_found'` 且 `fact_id` 为空——
    缺失用「格子是空的」表达，而不是「这一行不存在」。后者在页面上与
    「这家公司没披露这一项」长得一模一样，但成因完全不同。
    """

    model_config = ConfigDict(title="事实格")

    fact_id: str | None = None
    value: str | None = Field(default=None, description="字符串，见模块头说明")
    unit: str | None = None
    raw_unit: str | None = None
    status: str
    comparable: bool
    incomparable_reason: str | None = None
    source_file: str | None = None
    source_page: int | None = None
    confidence: float | None = None


class FactGridRowView(BaseModel):
    model_config = ConfigDict(title="事实网格行")

    metric_key: str
    label_cn: str
    statement: str
    unit_kind: str
    cells: dict[str, FactCellView] = Field(description="期间 → 格子")


class FactGridView(BaseModel):
    """「指标 × 年度」网格。

    读的是 `v_fact_grid_company` 而不是 `v_fact_grid`——后者硬编码
    `is_primary = 1`，会让华菱、首钢的网格每格都是 not_found 且不报错。
    """

    model_config = ConfigDict(title="事实网格")

    project_id: str
    company_name: str
    scope: str
    periods: list[str]
    metrics: list[FactGridRowView]


# ------------------------------------------------------------------ 事实与原文


class FactDetailView(BaseModel):
    """一笔财务事实的全部字段（`GET /api/facts/{id}`）。

    **证据链的终点**：`source_file` / `source_page` / `source_text` 三个字段
    就是「点任意结论回到年报原文」落地的地方，缺一个这条链就断在这里。
    """

    model_config = ConfigDict(title="事实详情")

    fact_id: str
    project_id: str
    company_id: str
    is_primary: bool
    metric: str
    value: str | None = None
    unit: str | None = None
    period: str
    scope: str
    source_file: str
    source_page: int
    source_text: str
    confidence: float
    status: str
    period_kind: str
    value_raw: str | None = None
    raw_unit: str | None = None
    unit_factor: str | None = None
    source_table: str | None = None
    source_row_label: str | None = None
    source_printed_page: str | None = None
    bbox: str | None = None
    extractor: str
    comparable: bool
    incomparable_reason: str | None = None
    restated: bool
    file_name: str | None = None
    file_period: str | None = None
    sign_basis: str | None = None
    mapped_from: str | None = None


class PageDetailView(BaseModel):
    """年报的一页正文（`GET /api/pages/{id}`、`GET /api/facts/{id}/page`）。

    返回的是**已抽取的正文**而不是 PDF 切片：样例 PDF 有几十 MB，被 .gitignore
    挡在仓库外，评委 clone 下来根本没有那些文件。正文在库里、能全文检索、
    能高亮，比 PDF 更好用。

    `prev_page_id` / `next_page_id` 由**后端**给出。前端不自己拼 id——
    那种隐式约定一旦和后端的 id 生成规则分叉，表现为「翻页点了没反应」，
    而且不报错。
    """

    model_config = ConfigDict(title="年报页")

    page_id: str
    file_id: str
    file_name: str | None = None
    period: str
    role: str
    page_no: int
    printed_page_no: str | None = None
    text: str | None = None
    text_source: str = Field(description="'native' 或 'ocr'")
    has_table: bool
    prev_page_id: str | None = None
    next_page_id: str | None = None
    first_page_no: int | None = None
    last_page_no: int | None = None


# ------------------------------------------------------------------ 勾稽校验


class CheckSummaryView(BaseModel):
    """勾稽校验的汇总。

    `hard_failed` 与 `soft_failed` 分开是刻意的：报表本身不平（几可断定解析
    错了）与字典缺字段（报表没问题，是字典没覆盖）是两件事，混成一个
    「失败数」会让前者被后者的噪声淹没。
    """

    model_config = ConfigDict(title="勾稽汇总")

    total: int
    evaluable: int
    passed: int
    failed: int
    hard_failed: int
    soft_failed: int
    skipped_missing_data: int
    skipped_incomparable: int
    sheet_ok: bool
    coverage_line: str


class CheckResultView(BaseModel):
    """一条勾稽结论。`formula` 与 `inputs` 是「可复算」的落点。"""

    model_config = ConfigDict(title="勾稽结论")

    rule_key: str
    period: str
    scope: str
    status: str
    severity: str
    lhs: str | None = None
    rhs: str | None = None
    diff: str | None = None
    tolerance: str | None = None
    formula: str
    inputs: list[Any] = Field(default_factory=list)
    message: str
    suggestion: str | None = None


class ChecksView(BaseModel):
    """`GET /api/checks` 的响应。

    `warnings` 记的是**取数阶段**的问题（值解析不出来、同一指标多行等），
    与校验结论分开：把它们混进 `results` 会让「报表不平」和「这行没读出来」
    看起来是同一种失败。
    """

    model_config = ConfigDict(title="勾稽校验")

    project_id: str
    scope: str
    method_version: str
    summary: CheckSummaryView
    results: list[CheckResultView]
    warnings: list[Any] = Field(default_factory=list)


class StoredChecksView(BaseModel):
    """`GET /api/checks/stored`：读回上次落库的结果，供「与上次运行对照」。"""

    model_config = ConfigDict(title="上次勾稽结果")

    project_id: str
    results: list[CheckResultView]


# ------------------------------------------------------------------ 叙事一致性


class ClaimView(BaseModel):
    """一条主张（MD&A 里的一句话结构化之后）。

    `verifiable` / `background_only` 用 0/1 而不是布尔：它们直接来自 SQLite
    的 INTEGER 列，且页面上要按它们求和。
    """

    model_config = ConfigDict(title="主张")

    claim_id: str
    claim_text: str
    claim_type: str
    direction: str
    period_norm: str | None = None
    period_expr: str | None = None
    magnitude_text: str | None = None
    magnitude_value: str | None = None
    magnitude_unit: str | None = None
    verifiable: int
    background_only: int
    confidence: float
    status: str
    source_page: int
    primary_metric: str | None = None
    forbidden_simplifications: list[str] = Field(
        default_factory=list,
        description="该主题被禁止的简化推断，随主张一起展示供人工复核对照",
    )


class ClaimStatsView(BaseModel):
    model_config = ConfigDict(title="主张统计")

    total: int
    verifiable: int
    background_only: int
    validated: int
    by_type: dict[str, int] = Field(default_factory=dict)


class ExtractorSummaryView(BaseModel):
    """一种抽取法的概况，用来做「规则法 vs LLM」的并排对照。

    条数少不代表差、也不代表好——LLM 版条数少于规则法（它更挑），但可验证的
    比例更高。**这两件事都不该由代码下结论**，并排放着让人自己判。
    """

    model_config = ConfigDict(title="抽取法对照")

    extractor: str = Field(
        description="'rule:claim_v1' 或 'llm:deepseek-chat@<prompt_hash>'"
    )
    total: int
    verifiable: int
    validated: int
    theme_count: int


class ClaimsView(BaseModel):
    model_config = ConfigDict(title="主张列表")

    project_id: str
    stats: ClaimStatsView
    by_extractor: list[ExtractorSummaryView]
    claims: list[ClaimView]


class ClaimMatchView(BaseModel):
    """一条「主张 → 事实」的判定。

    `formula` 与 `inputs` 允许为空：不可比的主张没有算式可给，
    给一个空算式比编一个更有用。

    `target_unit` / `target_millions` / `unit_factor` 只在**绝对量目标**上非空：
    目标写「亿元」而事实库存「百万元」，页面要把这一步换算显示出来，
    否则用户只看到一个换算过的数、看不出它从哪来（会计口径 8-1）。
    `plan_variance` 是「原始计划偏差」，8-3 第 4 条允许展示、
    **但不进 H 的支持/相悖判定**——它的 verdict 一定是 needs_review。
    `plan_reference` 是按 8-2 的方向算出的参考结论，是**文字参考不是判定**。
    """

    model_config = ConfigDict(title="主张判定")

    match_id: str
    claim_id: str
    metric_key: str
    verdict: str
    reason: str
    confidence: float
    claim_period: str
    fact_period: str
    direction_claim: str | None = None
    direction_actual: str | None = None
    magnitude_target: str | None = None
    magnitude_actual: str | None = None
    relative_deviation: str | None = None
    formula: str | None = None
    inputs: str | None = None
    target_unit: str | None = None
    target_millions: str | None = None
    unit_factor: str | None = None
    plan_variance: str | None = None
    plan_reference: str | None = None
    claim_text: str
    claim_type: str
    source_page: int
    verifiable: int
    background_only: int


class MatchesView(BaseModel):
    model_config = ConfigDict(title="判定结果")

    project_id: str
    counts: dict[str, int] = Field(default_factory=dict)
    hint: str | None = Field(default=None, description="没跑过匹配时给出的说明")
    matches: list[ClaimMatchView]


# ------------------------------------------------------------------ 诊断指数


class IndexComponentsView(BaseModel):
    """诊断指数的五个构成项。

    **每一项都是 [−1, 1] 区间的字符串**（会计口径 v1.1 §一）。
    旧稿五个分项都是 0–1，中性证据只能取 0.5，于是 H=C=0.5 会算出
    50 + 20×0.5 + 20×0.5 = 70 分——正好压在「一致性较高」的分界线上，
    全中性的公司反而得分最高。
    """

    model_config = ConfigDict(title="指数构成项")

    history: str | None = None
    current: str | None = None
    risk: str | None = None
    template: str | None = None
    quality: str | None = None


class IndexCountsView(BaseModel):
    """分母与跳过数。

    跳过了什么**也要说**，不只报成功的数——只报成功的数会让「公司没披露」
    和「系统没读到」看起来一样。
    """

    model_config = ConfigDict(title="指数计数")

    n: int
    N: int
    history_observations: int
    current_observations: int
    skipped_no_period: int
    skipped_no_fact: int


class ScenarioMappingView(BaseModel):
    """指数 → 估值情景的传导。

    **没有目标价字段，也不该有。** 估值输出永远是区间；单点目标价会隐藏
    不确定性，而这里给的是「有一组证据指向增长假设需要复核」。
    """

    model_config = ConfigDict(title="情景传导")

    weights: dict[str, str] | None = Field(
        default=None,
        description="不足以出分时是 null——给一组「差不多的权重」会让闸门形同虚设",
    )
    revenue_growth_ref: str | None = None
    requires_human_confirmation: bool
    changes_valuation: bool
    notes: list[str] = Field(default_factory=list)


class NarrativeIndexView(BaseModel):
    """`GET /api/projects/{id}/narrative/index` 的响应。

    闸门不过时 `score` 是 **null**，并附逐条的未满足条件。页面据此显示
    「证据不足，不出分」——**绝不用 0 分或 50 分代替**：分母没变、权重照乘，
    那个分数看起来和完整版一模一样，而它缺了整整 30 分权重的构成项。
    """

    model_config = ConfigDict(title="诊断指数")

    project_id: str
    status: str = Field(description="'scored' 或 'insufficient_evidence'")
    grade: str
    score: str | None = Field(default=None, description="字符串；闸门不过时是 null")
    components: IndexComponentsView
    coverage: str | None = None
    counts: IndexCountsView
    insufficient_reason: str | None = None
    formula: str
    conclusion_boundary: str
    scenarios: ScenarioMappingView
    action: str
    valuation_action: str
    user_hint: str
    method_version: str


# ------------------------------------------------------------------ 规则参数


class RuleConfigView(BaseModel):
    """一条口径参数。页面提供查看/修改/恢复默认，改的就是这些行。

    ⚠ **这里没有 `tier` 字段，虽然 `rule_config` 表里有这一列。**
    `repository.list_rule_config()` 的 SELECT 没有取它，页面也就拿不到——
    而 `tier` 决定改这个值要谁点头（A-8：`hard` 会计口径须签字 / `soft`
    提示语与排序 / `model` 模型参数）。要做「这个值归谁批」的界面时，
    得先把那一列加进 SELECT，模型这里同步加。

    ⚠ 加这个模型时我照着表结构写了个 `tier: str`，接口直接 500
    （`ResponseValidationError: Field required`）——**这正是 `response_model`
    该有的行为**：少一个字段立刻炸，而不是像 `dict[str, Any]` 那样
    悄悄少返回一个字段、前端拿到 `undefined` 还不报错。
    """

    model_config = ConfigDict(title="口径参数")

    key: str
    value: str
    value_type: str
    industry: str
    label_cn: str
    description: str
    unit: str
    default_value: str | None = None
    min_value: str | None = None
    max_value: str | None = None
