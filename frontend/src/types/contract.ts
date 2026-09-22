/* 本文件由 backend/scripts/export_schemas.py 自动生成，请勿手改。
 *
 * 数据契约的唯一定义源在 backend/app/schemas/（Pydantic v2）。
 * 契约变更后请重新运行：
 *     cd backend && python scripts/export_schemas.py
 *
 * 枚举导出成**字面量联合类型**而不是 TS `enum`：
 * 一是 `tsconfig.app.json` 开了 `erasableSyntaxOnly`，`enum` 会被编译拒绝；
 * 二是联合类型在收窄和穷尽检查上更好用，而且和 JSON 里的取值是同一批字符串。
 */

/**
 * SSE 事件类型白名单。与 backend/app/agents/state.py::EVENT_TYPES 同源。
 *
 * 服务端发的是**具名事件**（`event: step.started`），不会触发 EventSource
 * 的 onmessage，必须按类型逐个 addEventListener。所以前端需要这份清单——
 * 也就意味着它不能靠手抄：漏一个类型的后果是那一格**静默不显示**。
 */
export const EVENT_TYPES = [
  "evidence.attached",
  "heartbeat",
  "llm.called",
  "llm.result",
  "plan.confirmed",
  "plan.created",
  "progress",
  "review.required",
  "step.failed",
  "step.retrying",
  "step.skipped",
  "step.started",
  "step.succeeded",
  "task.completed",
  "task.failed",
  "tool.called",
  "tool.result",
  "warning",
] as const;

export type TaskEventType = (typeof EVENT_TYPES)[number];

/** 收到这几个事件就意味着任务结束了，流会关掉。 */
export const TASK_ENDING_EVENTS: readonly TaskEventType[] = ["task.completed", "task.failed"];

export type AdjustmentCategory = "gov_subsidy" | "asset_disposal" | "investment_income" | "fair_value_change" | "hedging" | "impairment" | "restructuring" | "related_party" | "other";

export type AdjustmentDirection = "add_back" | "deduct";

export type AdjustmentReviewStatus = "pending" | "approved" | "rejected";

/**
 * 结构化应用日志。
 * 
 * 主存数据库而非文件：`.gitignore` 忽略了 `logs`，写文件的日志不会随项目导出，
 * 也无法在页面上按 task_id 查询。
 */
export interface AppLog {
  id?: number | null;
  ts: string;
  level: LogLevel;
  logger: string;
  event: string;
  request_id?: string | null;
  task_id?: string | null;
  step_id?: string | null;
  message: string;
  payload?: Record<string, unknown> | null;
  duration_ms?: number | null;
}

/** 研究报告纠错的一条发现。 */
export interface AuditFinding {
  finding_id: string;
  report_id: string;
  original_text: string;
  issue_type: AuditIssueType;
  expected_value?: string | null;
  evidence_id?: string | null;
  severity: Severity;
  fix_suggestion: string;
  status?: AuditFindingStatus;
  created_at: string;
}

export type AuditFindingStatus = "confirmed" | "needs_review" | "ignored";

export type AuditIssueType = "number" | "unit" | "period" | "scope" | "multiple" | "citation" | "opinion_mixed";

/**
 * 一条勾稽/校验规则的执行结果。
 * 
 * 每条都带公式与输入 fact_id 列表，构成「结论 → 计算 → 字段 → 页码 → 原文」的证据链。
 */
export interface CheckResult {
  check_id: string;
  project_id: string;
  period: string;
  scope: Scope;
  /** 如 'bs_equation' / 'ni_to_cfo_bridge' / 'cash_rollforward' */
  rule_key: string;
  severity: Severity;
  status: string;
  lhs?: string | null;
  rhs?: string | null;
  diff?: string | null;
  tolerance?: string | null;
  formula: string;
  /** 参与计算的 fact_id */
  input_facts?: string[];
  /** 中文可读说明，页面直接展示 */
  message: string;
  suggestion?: string | null;
  created_at: string;
}

/**
 * 一条管理层主张。
 * 
 * `claim_text` 必须是**原句，不得改写**——证据链的终点就是这句话在年报里的位置。
 */
export interface Claim {
  claim_id: string;
  project_id: string;
  section_id: string;
  /** 原句，不改写 */
  claim_text: string;
  subject?: string | null;
  action?: string | null;
  object?: string | null;
  /** 原文期间表述，如「2024 年」 */
  period_expr?: string | null;
  /** 归一化期间 */
  period_norm?: string | null;
  direction?: ClaimDirection;
  /** 如「20% 以上」 */
  magnitude_text?: string | null;
  magnitude_value?: string | null;
  magnitude_unit?: string | null;
  claim_type: ClaimType;
  /** 无法识别期间/对象时为 False */
  verifiable: boolean;
  /** 为 True 时仅作背景展示，不进入一致性评分 */
  background_only?: boolean;
  confidence: number;
  source_file_id: string;
  source_page: number;
  source_text: string;
  bbox?: string | null;
  extractor: string;
  prompt_version: string;
  /** 抽取这条主张的那次模型调用，可回溯用的 prompt 原文 */
  llm_call_id?: string | null;
  status: FactStatus;
  created_at: string;
}

export type ClaimDirection = "up" | "down" | "improve" | "deteriorate" | "flat" | "unknown";

/** 一条主张对应的候选指标。一条主张可以有多个候选。 */
export interface ClaimIndicator {
  id: string;
  claim_id: string;
  metric_key: string;
  /** 'primary' 或 'supporting' */
  role: string;
  match_confidence: number;
  /** 'rule:alias' / 'llm:<prompt>' / 'human:<uid>'，规则优先 */
  matched_by: string;
}

/** 主张 × 指标 × 期间 三元组——诊断指数的基本观测单位。 */
export interface ClaimMatch {
  match_id: string;
  claim_id: string;
  metric_key: string;
  fact_id?: string | null;
  claim_period: string;
  fact_period: string;
  direction_claim?: ClaimDirection | null;
  direction_actual?: ClaimDirection | null;
  direction_consistent?: boolean | null;
  magnitude_target?: string | null;
  magnitude_actual?: string | null;
  relative_deviation?: string | null;
  verdict: MatchVerdict;
  /** 中文理由，页面直接展示 */
  reason: string;
  confidence: number;
  formula?: string | null;
  /** 参与计算的 fact_id 与取值，保证可复算 */
  inputs?: Record<string, string> | null;
  reviewer?: string | null;
  reviewed_at?: string | null;
  created_at: string;
}

/** MD&A 主张的主题。对应大框架里的四类叙事信号。 */
export type ClaimType = "demand" | "order" | "capacity" | "collection" | "product_mix" | "cost" | "risk" | "macro" | "other";

/**
 * 可比公司。
 * 
 * 煤价是钢企的成本项，两者周期驱动因素相反。把煤企当作钢企的估值可比公司，
 * 中位数会失去意义——所以产业链上游公司只能以 chain_reference 身份出现。
 */
export interface ComparableCompany {
  comparable_id: string;
  project_id: string;
  name: string;
  stock_code?: string | null;
  industry: string;
  sub_industry?: string | null;
  business_similarity: string;
  scale_note?: string | null;
  growth_note?: string | null;
  profitability: Profitability;
  data_date: string;
  /** 选择理由必须记录 */
  selection_reason: string;
  peer_role?: PeerRole;
  pe?: string | null;
  ev_ebitda?: string | null;
  pb?: string | null;
  source: string;
  excluded?: boolean;
  exclude_reason?: string | null;
}

/** 当前处于周期什么位置。必须显式标注，不能假装周期不存在。 */
export type CyclePosition = "peak" | "above_mid" | "mid" | "below_mid" | "trough" | "unknown";

export interface DatabaseStatus {
  ok: boolean;
  path: string;
  counts?: Record<string, number>;
  error?: string | null;
}

/** 指数的构成项。页面据此展示「这个分是怎么来的」。 */
export interface DiagnosisComponent {
  id: string;
  run_id: string;
  /** history / current / risk_shift / template_penalty / quality_conflict */
  component: string;
  raw_value?: string | null;
  weight?: string | null;
  contribution?: string | null;
  formula: string;
  /** 触发本项的 claim_ids / fact_ids / check_ids / match_ids */
  evidence_refs?: Record<string, string[]>;
  /** 中文说明，模板生成而非 LLM 生成 */
  explanation: string;
}

/** 一次诊断指数计算的结果。 */
export interface DiagnosisRun {
  run_id: string;
  project_id: string;
  task_id?: string | null;
  /** 引用规则版本 → 同输入可复现同结果 */
  rule_config_version: number;
  observation_count: number;
  /** 进入分母的有效观测 = supported + partial + conflicted */
  comparable_count: number;
  /** 不扣分 */
  incomparable_count?: number;
  /** 不推断失败，转人工复核 */
  missing_count?: number;
  /** 方向一致但幅度偏弱 */
  partial_count?: number;
  /** 置信度加权覆盖率 = Σconf(有效观测) / Σconf(全部可验证主张) */
  coverage?: string | null;
  /** 0–100；证据不足时为 None */
  score?: string | null;
  grade: IndexGrade;
  confidence?: number | null;
  insufficient_reason?: string | null;
  /** 审慎表述模板。禁止输出「管理层叙事虚假」等超出证据范围的结论 */
  conclusion_boundary: string;
  components?: DiagnosisComponent[];
  created_at: string;
}

/** 一笔 EBIT 调整。每一笔都必须留下完整审计痕迹。 */
export interface EbitAdjustment {
  adjustment_id: string;
  normalization_year_id: string;
  /** 原始项目名称，原样保留年报里的写法 */
  item: string;
  category: AdjustmentCategory;
  /** 调整金额（正数） */
  amount: string;
  direction: AdjustmentDirection;
  /** 调整理由，必须可读 */
  reason: string;
  source_file?: string | null;
  source_page?: number | null;
  review_status?: AdjustmentReviewStatus;
  reviewer?: string | null;
  reviewed_at?: string | null;
  created_at: string;
}

/** Reported 为 DCF 默认；Adjusted 需会计逐笔批准后才可切换。 */
export type EbitVariant = "reported" | "adjusted";

/**
 * 一次性返回已发出的 SSE 事件。
 * 
 * 给两个场景用：页面刷新后先拉历史再订阅实时流；以及排查「前端少了一格」时
 * 对照服务端到底发了什么。
 */
export interface EventHistoryResponse {
  count: number;
  events: TaskEvent[];
}

/**
 * 一条证据指针。
 * 
 * `ref_table` + `ref_id` 指向系统里任何一个可被引用的对象（事实、主张、匹配、
 * 校验、计算、页面……），`file_id` + `page_no` + `quote` 指向它在年报里的出处。
 */
export interface Evidence {
  evidence_id: string;
  kind: EvidenceKind;
  ref_table: string;
  ref_id: string;
  file_id?: string | null;
  page_no?: number | null;
  bbox?: string | null;
  quote?: string | null;
  /** 给用户看的出处描述，如「2024年报 p.86 合并现金流量表」 */
  label_cn: string;
  created_at: string;
}

export type EvidenceKind = "fact" | "claim" | "match" | "check" | "calc" | "llm" | "page" | "citation" | "peer" | "benchmark";

/**
 * 字段字典里例句的来源。
 * 
 * `synthetic_example` 是带【数值】占位符的标准句，只供解析器回归测试用，
 * **不得**在界面或报告里当作年报原文展示。
 */
export type ExampleSource = "annual_report" | "synthetic_example";

/** 人工修正。只追加增量，旧行保留，绝不原地覆盖。 */
export interface FactCorrection {
  correction_id: string;
  fact_id: string;
  field: string;
  old_value?: string | null;
  new_value?: string | null;
  reason: string;
  operator: string;
  created_at: string;
}

/**
 * 同一笔事实在年报里的一次出现。
 * 
 * 同一个数字往往同时出现在主要指标表、三张主表、附注和正文里，精度还可能不同。
 * 每次出现各记一条观测，交叉校验后把被采纳的那条标为 adopted 并回填 fact_id，
 * 其余必须写明未采纳理由。
 */
export interface FactObservation {
  observation_id: string;
  project_id: string;
  company_id: string;
  metric: string;
  period: string;
  period_kind: PeriodKind;
  scope: Scope;
  /** 原样文本，如 '12,345,678,901.23' */
  value_raw: string;
  raw_unit?: string | null;
  value?: string | null;
  source_file_id: string;
  source_page: number;
  source_table?: string | null;
  source_location: SourceLocation;
  source_text: string;
  bbox?: string | null;
  confidence: number;
  extractor: string;
  created_at: string;
  resolution?: ObservationResolution;
  resolved_fact_id?: string | null;
  rejection_note?: string | null;
}

export type FactStatus = "validated" | "needs_review" | "not_found" | "rejected";

/** 一次文件访问。「完整记录文件访问」的落点。 */
export interface FileAccessLog {
  id?: number | null;
  ts: string;
  /** 'user:<uid>' / 'tool:<name>' / 'mcp:<server>' */
  actor: string;
  file_id?: string | null;
  /** 相对 DATA_ROOT 的路径；绝对路径不入库 */
  rel_path: string;
  /** read / open / preview / export / deny */
  action: string;
  page_no?: number | null;
  task_id?: string | null;
  tool_call_id?: string | null;
  sha256_verified?: boolean | null;
  allowed: boolean;
  deny_reason?: string | null;
}

export interface FileListResponse {
  files: FileView[];
}

export type FileRole = "annual_report" | "half_year" | "quarterly" | "announcement" | "comparable" | "research_draft" | "industry_data";

/**
 * 登记进项目的一份文件。
 * 
 * `parse_status` 停在 'pending' 是**诚实的状态**：页面据此显示「待解析」，
 * 而不是把没解析过的文件显示成「已解析但没数据」。
 */
export interface FileView {
  file_id: string;
  project_id: string;
  role: FileRole;
  /** '2024' / '2024H1' / '2024Q3' */
  period: string;
  /** 相对 DATA_ROOT；绝对路径禁止入库 */
  rel_path: string;
  sha256: string;
  bytes: number;
  page_count?: number | null;
  /** 扫描件需走 OCR 通道，精度不同 */
  is_scanned: boolean;
  parse_status: ParseStatus;
  parse_error?: string | null;
  uploaded_at: string;
}

/**
 * 一条已验证或待复核的财务事实。
 * 
 * 十个契约字段：metric / value / unit / period / scope / source_file /
 * source_page / source_text / confidence / status。
 */
export interface FinancialFact {
  fact_id: string;
  project_id: string;
  company_id: string;
  is_primary: boolean;
  /** 契约名；数据库列是 metric_key */
  metric: string;
  /** 契约名；数据库列是 value_millions。单位统一为百万元。缺失时为 None，**绝不为 0** */
  value?: string | null;
  unit?: string;
  /** '2024' / '2024H1' / '2024-12-31' */
  period: string;
  scope: Scope;
  source_file: string;
  source_page: number;
  source_text: string;
  confidence: number;
  status: FactStatus;
  period_kind: PeriodKind;
  period_start?: string | null;
  period_end?: string | null;
  value_raw?: string | null;
  raw_unit?: string | null;
  unit_factor?: string | null;
  source_file_id: string;
  /** 页脚印刷页码；与 PDF 物理页序常不一致，必须分开存 */
  source_printed_page?: string | null;
  source_table?: string | null;
  /** 原始行名，保留原始标签 */
  source_row_label?: string | null;
  /** 来源映射（签字文档 A-2）：本行的值实际抽自哪一个字段。只披露「营业总收入」的年度，其值映射到 revenue 并记 mapped_from='total_revenue'。按 (metric, period) 聚合前必须看这一列——否则 revenue 与 total_revenue 会被重复计入 */
  mapped_from?: string | null;
  bbox?: string | null;
  /** 'rule:v3' / 'llm:deepseek-chat@<prompt_hash>' / 'human:<uid>' */
  extractor: string;
  restated?: boolean;
  restatement_note?: string | null;
  comparable?: boolean;
  incomparable_reason?: IncomparableReason | null;
  created_at: string;
}

/**
 * 健康检查。
 * 
 * 数据库不可用时返回的是 **503 + `status='degraded'`**，不是 200——
 * 一个永远返回 200 的健康检查等于没有健康检查。
 */
export interface HealthResponse {
  /** 'ok' 或 'degraded' */
  status: string;
  database: DatabaseStatus;
  /** 离线回放模式；为真时界面必须显著标注 */
  offline_mode: boolean;
  llm_configured: boolean;
  /** 使用的模型名，如 'deepseek-chat' */
  model: string;
}

/**
 * 不可直接比较的原因。标记后不进指数扣分，但从主中枢中排除。
 * 
 * ⚠ 取值必须与 `schema.sql` 里 `financial_fact` 和 `normalization_year` 两张表的
 *   `incomparable_reason` CHECK 完全一致。这三处曾经分叉过：Pydantic 放行
 *   `asset_injection`，而 `financial_fact` 的 CHECK 里没有它——于是签字文档 §4
 *   明确要求标记的「资产注入年度」**写不进去**，报的还是一句 constraint failed。
 *   `tests/unit/schemas/test_contract.py` 里有对拍测试盯着这三处。
 */
export type IncomparableReason = "mna" | "restructuring" | "asset_injection" | "scope_change" | "restatement" | "policy_change" | "industry_cycle" | "seasonality" | "other";

export type IndexGrade = "high" | "medium" | "low" | "insufficient";

/**
 * 一次模型调用。
 * 
 * `output` 保存原始输出，配合 prompt_hash + input_hash 可以离线回放——
 * 现场断网时走预录响应，**界面必须显著标注「离线回放模式」**。
 */
export interface LlmCall {
  call_id: string;
  task_id?: string | null;
  step_id?: string | null;
  /** intent / plan / claim_extract / memo_text */
  purpose: string;
  model: string;
  model_version?: string | null;
  prompt_key: string;
  prompt_version: string;
  prompt_hash: string;
  /** temperature/seed 等，**不含密钥** */
  params?: Record<string, unknown>;
  input_hash: string;
  input_digest?: string | null;
  output?: string | null;
  tokens_in?: number | null;
  tokens_out?: number | null;
  latency_ms?: number | null;
  cached?: boolean;
  /** 离线回放的来源 */
  cassette_id?: string | null;
  status: string;
  error?: string | null;
  created_at: string;
}

export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

/**
 * 主张—事实匹配的四态 + 部分支持。
 * 
 * `partial` 用于「方向一致但幅度明显偏弱」，避免把只兑现一半与完全兑现混为一谈。
 */
export type MatchVerdict = "supported" | "partial" | "conflicted" | "incomparable" | "missing";

/** MD&A 章节切分结果。 */
export interface MdnaSection {
  section_id: string;
  file_id: string;
  heading?: string | null;
  kind: MdnaSectionKind;
  page_from: number;
  page_to: number;
  text: string;
}

export type MdnaSectionKind = "mdna" | "risk_disclosure" | "business_review" | "outlook" | "other";

/** 运行环境。密钥已脱敏（只显示是否配置,不显示值）。 */
export interface MetaResponse {
  backend_dir: string;
  data_root: string;
  settings: Record<string, unknown>;
}

/** 字段字典的一条。aliases / exclusion_terms 由会计同学维护。 */
export interface MetricDefinition {
  /** 机器可读的指标键，如 'revenue' */
  metric_key: string;
  label_cn: string;
  /** 年报表格里的原始行名 */
  aliases?: string[];
  /** 行名**精确等于**这些词时不得映射到本指标，用于挡住「营业成本率 → 营业成本」这类同数量纲的误映射 */
  exclusion_terms?: string[];
  statement: Statement;
  value_type: ValueType;
  unit_kind: UnitKind;
  sign_convention: SignConvention;
  is_nonrecurring?: boolean;
  is_derived?: boolean;
  industry?: string | null;
  /** 「其中：」层级的父指标 */
  parent_key?: string | null;
  example_sentence?: string | null;
  /** 例句来源。synthetic_example 表示仍是占位符标准句，不得当作真实证据 */
  example_source?: ExampleSource | null;
  /** 例句所在的 PDF 文件名。example_source=annual_report 时必填 */
  example_file?: string | null;
  /** 例句所在页码。example_source=annual_report 时必填 */
  example_page?: number | null;
  scope_note?: string | null;
}

/**
 * 交叉验证项：吨钢毛利 / EBITDA margin / 产能利用率 / ROIC。
 * 
 * **一律不影响 DCF。** 核心中枢只能来自 NormalizationRun.ebit_margin_mid。
 */
export interface NormalizationCrosscheck {
  id: string;
  normalization_id: string;
  metric_key: string;
  /** 同一指标的多套口径分开存，禁止混用 */
  variant?: string;
  mid_cycle_value?: string | null;
  unit?: string | null;
  /** 原样记录数据来源口径 */
  basis?: string | null;
  /** company_disclosed / self_calculated / external */
  data_source: string;
  agrees_with_core?: boolean | null;
  deviation_note?: string | null;
  /** 固定为 False */
  affects_dcf?: boolean;
  evidence_refs?: Record<string, string[]> | null;
  created_at: string;
}

/** 一次周期正常化运行。 */
export interface NormalizationRun {
  normalization_id: string;
  project_id: string;
  task_id?: string | null;
  window_mode: WindowMode;
  preferred_years?: number;
  fallback_years?: number;
  min_comparable_years: number;
  /** 最新完整年度，窗口据此滚动 */
  anchor_year?: string | null;
  window_start?: string | null;
  window_end?: string | null;
  years_available?: number | null;
  comparable_years?: number | null;
  covers_high_phase?: boolean | null;
  covers_low_phase?: boolean | null;
  coverage_passed?: boolean | null;
  high_threshold?: string | null;
  low_threshold?: string | null;
  coverage_detail?: Record<string, unknown> | null;
  ebit_variant?: EbitVariant;
  ebit_formula: string;
  crosscheck_formula?: string | null;
  crosscheck_max_deviation?: string | null;
  crosscheck_needs_review?: boolean;
  core_metric?: string;
  weighting?: string;
  /** 收入加权的周期中位 EBIT margin。这是 DCF 的唯一起点 */
  ebit_margin_mid?: string | null;
  ebit_margin_p25?: string | null;
  ebit_margin_p75?: string | null;
  status: NormalizationStatus;
  insufficient_reason?: string | null;
  method_version: string;
  rule_config_version: number;
  years?: NormalizationYear[];
  created_at: string;
}

export type NormalizationStatus = "normalized" | "extended_normalized" | "NORMALIZATION_INSUFFICIENT_DATA" | "incomplete_cycle";

/** 窗口内一年的明细。三种 EBIT 并存，禁止只留一个。 */
export interface NormalizationYear {
  id: string;
  normalization_id: string;
  year: string;
  revenue?: string | null;
  profit_before_tax?: string | null;
  interest_expense?: string | null;
  interest_income?: string | null;
  /** 交叉核对用 */
  operating_profit?: string | null;
  /** 交叉核对用 */
  finance_expense?: string | null;
  reported_ebit?: string | null;
  crosscheck_ebit?: string | null;
  adjusted_ebit?: string | null;
  crosscheck_deviation?: string | null;
  adjustment_delta?: string | null;
  /** 会计是否已批准；未批准时主 DCF 只能用 reported */
  adjustment_approved?: boolean;
  ebit_margin?: string | null;
  ebit_variant_used?: EbitVariant | null;
  revenue_weight?: string | null;
  /** high / normal / low */
  phase?: string | null;
  is_median_year?: boolean | null;
  comparable?: boolean;
  incomparable_reason?: string | null;
  reviewed_by_accounting?: boolean;
  included_in_median?: boolean;
  revenue_fact_id?: string | null;
  ebit_fact_id?: string | null;
  note?: string | null;
}

/** 同一次取值的多个观测，裁决结果。 */
export type ObservationResolution = "pending" | "adopted" | "rejected";

/** 估值参数的来源。禁止模型凭记忆填入。 */
export type ParamSourceType = "historical_fact" | "user_input" | "comparable_stat" | "model_assumption";

export type ParseStatus = "pending" | "parsing" | "parsed" | "failed";

/**
 * 可比公司的角色。
 * 
 * `chain_reference`（产业链上游参照）**不得进入估值可比集**——
 * 煤价是钢企的成本项，两者周期驱动因素相反，混进中位数会让它失去意义。
 */
export type PeerRole = "valuation_peer" | "chain_reference";

/**
 * 数值本身的性质，不是它出现在哪份报告里。
 * 
 * 刻意不含 'prior'：FY2023 的数字无论在 2023 年报的「本期」栏还是 2024 年报的
 * 「上期」栏，都是同一笔事实，period='2023' + kind='current'。多一个 kind 会让
 * 同一笔事实存成两行，聚合时重复计算。
 */
export type PeriodKind = "current" | "instant" | "opening" | "average";

export type Profitability = "profitable" | "loss";

export interface ProjectListResponse {
  projects: ProjectView[];
}

/** 一个投研项目（一家公司的若干年年报）。 */
export interface ProjectView {
  project_id: string;
  name: string;
  company_name: string;
  /** 如 '600019.SH' */
  stock_code: string;
  /** 'steel'；本次参赛只跑钢铁 */
  industry: string;
  base_currency: string;
  /** 覆盖的会计年度。库里是 JSON 文本，出库时转成数组 */
  fiscal_years?: string[];
  /** 默认会计口径：consolidated / parent */
  base_scope: string;
  /** 'active' 或 'archived' */
  status: string;
  created_at: string;
}

/**
 * 一份生成的报告（备忘录 / 纠错清单 / 分析）。
 * 
 * 备忘录使用固定九段模板，**不让模型自由发挥结构**——结构一自由，风险与证伪
 * 条件这类「不写也没人发现」的段落就会消失。
 */
export interface Report {
  report_id: string;
  project_id: string;
  kind: ReportKind;
  template_key: string;
  template_version: string;
  version: number;
  payload_md: string;
  payload_json?: Record<string, unknown>;
  manifest_id?: string | null;
  citations?: ReportCitation[];
  created_at: string;
}

/**
 * 报告正文里的一个引用锚点。
 * 
 * `number_ref` 记录被引用的数字，用于校验「正文写的数」与「证据里的数」是否一致——
 * 这是防止模型在组织文字时悄悄改数的最后一道检查。
 */
export interface ReportCitation {
  id: string;
  report_id: string;
  /** 正文中的锚点，如 '[E12]' */
  anchor: string;
  number_ref?: string | null;
  evidence_id: string;
  rendered_text: string;
}

export type ReportKind = "memo" | "audit" | "analysis";

/** 一条规则参数。页面提供查看 / 修改 / 恢复默认。 */
export interface RuleConfigItem {
  key: string;
  /** 空串表示全局默认；填具体行业则只覆盖该行业 */
  industry?: string;
  value: string;
  value_type: string;
  label_cn: string;
  description: string;
  unit?: string | null;
  /** 「恢复默认」的依据 */
  default_value: string;
  min_value?: string | null;
  max_value?: string | null;
}

/**
 * 复现一次的完整凭据。
 * 
 * 「同一份输入和配置可以重新生成同一计算结果」不是靠承诺，是靠这张表：
 * 代码版本 + 依赖锁哈希 + 输入文件 sha256 + 模型名版本 + prompt 注册表哈希 +
 * 规则版本 + 随机种子，缺一不可。
 */
export interface RunManifest {
  manifest_id: string;
  project_id?: string | null;
  task_id?: string | null;
  code_version: string;
  /** 工作区有未提交改动时为 True，提示结果可能不可复现 */
  code_dirty: boolean;
  python_version: string;
  deps_lock_hash: string;
  sample_pack_version?: string | null;
  model_name?: string | null;
  model_version?: string | null;
  prompt_registry_hash: string;
  rule_config_version: number;
  random_seed?: number | null;
  /** {file_id: sha256} */
  input_file_hashes?: Record<string, string>;
  output_hash?: string | null;
  offline_replay?: boolean;
  created_at: string;
}

/**
 * 诊断指数向估值情景的透明传导结果。
 * 
 * **纯函数的产物，绝不产出目标价。** 每次传导都要能回答
 * 「原参数是什么、新参数是什么、由哪条证据触发、适用范围到哪」。
 */
export interface ScenarioDelta {
  /** base / bull / bear */
  scenario: string;
  /** 情景权重，由映射规则给出而非手填 */
  weight: string;
  /** 如 'historical_p25'，指向 rule_config 里的取数规则 */
  revenue_growth_ref?: string | null;
  wacc_delta?: string | null;
  terminal_growth_delta?: string | null;
  /** 触发本次调整的 diagnosis_component.id */
  trigger_refs?: string[];
  /** grade=insufficient 或不可比占多数时为 False——不自动改变任何参数 */
  applied?: boolean;
  /** 用户可见的中文提示 */
  note: string;
}

/** 会计口径。默认合并口径；母公司口径只在用户明确选择时展示。 */
export type Scope = "consolidated" | "parent";

export type Severity = "info" | "warn" | "error";

export type SignConvention = "positive_is_good" | "negative_is_good" | "neutral";

export interface SkillListResponse {
  count: number;
  skills: SkillView[];
}

export interface SkillView {
  key: string;
  name: string;
  description: string;
}

/** 同一数字在年报中的位置。位置不同可信度不同，交叉校验时按此加权。 */
export type SourceLocation = "main_statement" | "indicator_table" | "notes" | "mdna_text" | "other";

export type Statement = "balance" | "income" | "cashflow" | "indicator" | "industry" | "disclosure";

export type StepStatus = "pending" | "running" | "succeeded" | "failed" | "skipped" | "retrying";

/**
 * 一次编排任务。
 * 
 * Agent 必须**先生成计划再执行**（plan 非空且 plan_confirmed），不允许边想边做——
 * 否则「执行过程可追溯」无从谈起。
 */
export interface Task {
  task_id: string;
  project_id?: string | null;
  /** 用户原话 */
  user_input: string;
  intent?: string | null;
  skill_key?: string | null;
  /** 步骤 + 依赖 + 工具 + 是否含 LLM */
  plan?: Record<string, unknown> | null;
  plan_confirmed?: boolean;
  status: TaskStatus;
  llm_plan_call_id?: string | null;
  error?: string | null;
  manifest_id?: string | null;
  steps?: TaskStep[];
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

/** SSE 事件。前端按 seq 游标重连，断线时带 Last-Event-ID 续传，保证时间线不丢帧。 */
export interface TaskEvent {
  seq: number;
  ts: string;
  task_id: string;
  step_id?: string | null;
  /** plan.created / step.started / step.succeeded / step.failed / tool.called / tool.result / llm.called / llm.result / progress / warning / evidence.attached / review.required / task.completed / heartbeat */
  type: string;
  payload?: Record<string, unknown>;
}

/** 任务列表（不含步骤，避免列表接口驮上全部时间线）。 */
export interface TaskListResponse {
  tasks: TaskSummary[];
}

/** 任务详情：任务本身 + 时间线步骤。 */
export interface TaskResponse {
  task: TaskSummary;
  steps: TaskStepView[];
}

export type TaskStatus = "pending" | "planned" | "running" | "waiting_confirm" | "succeeded" | "failed" | "cancelled";

/** 任务计划中的一步。 */
export interface TaskStep {
  step_id: string;
  task_id: string;
  seq: number;
  name: string;
  skill_key?: string | null;
  tool_name?: string | null;
  status: StepStatus;
  /** 依赖的 step_id，供前端画时间线 */
  depends_on?: string[];
  input_ref?: string | null;
  output_ref?: string | null;
  error?: string | null;
  retry_of?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

/**
 * 任务时间线上的一个步骤。
 * 
 * 是 `TaskStep` 的裁剪视图：只保留前端画时间线要用的字段，
 * 去掉 `input_ref` / `retry_of` 这类后端内部用的列。
 */
export interface TaskStepView {
  step_id: string;
  seq: number;
  name: string;
  tool?: string | null;
  status: string;
  depends_on?: string[];
  output?: string | null;
  error?: string | null;
}

/**
 * 任务本身，**不含步骤**。
 * 
 * 继承 `Task` 而不是另抄一份字段：抄一份的话，将来给 `Task` 加字段会漏掉这里，
 * 接口上就凭空少一个字段，而**没有任何地方会报错**。
 * `exclude=True` 让序列化时丢掉 `steps`——步骤单独放在响应体的 `steps` 里。
 */
export interface TaskSummary {
  task_id: string;
  project_id?: string | null;
  /** 用户原话 */
  user_input: string;
  intent?: string | null;
  skill_key?: string | null;
  /** 步骤 + 依赖 + 工具 + 是否含 LLM */
  plan?: Record<string, unknown> | null;
  plan_confirmed?: boolean;
  status: TaskStatus;
  llm_plan_call_id?: string | null;
  error?: string | null;
  manifest_id?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

/** 一次工具调用。 */
export interface ToolCall {
  call_id: string;
  task_id?: string | null;
  step_id?: string | null;
  tool_name: string;
  tool_version: string;
  transport: ToolTransport;
  /** True=纯计算由程序完成；False=含 LLM。前端据此区分「程序算的」与「模型理解的」，评审也能一眼看出边界 */
  deterministic: boolean;
  /** 入参（已脱敏） */
  args?: Record<string, unknown>;
  result_summary: string;
  result_hash?: string | null;
  status: string;
  duration_ms?: number | null;
  error?: string | null;
  created_at: string;
}

/** 一个任务的全部工具调用记录。 */
export interface ToolCallListResponse {
  count: number;
  calls: ToolCall[];
}

export interface ToolListResponse {
  count: number;
  tools: ToolSpecView[];
  /** 同一份 input_schema 经 MCP 暴露的样子 */
  mcp_preview?: Record<string, unknown>[];
}

/** 一个已登记的 Tool。 */
export interface ToolSpecView {
  name: string;
  version: string;
  description: string;
  /** True=纯计算由程序完成；False=含 LLM。评审据此看边界 */
  deterministic: boolean;
  side_effects: boolean;
  transport: ToolTransport;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
}

export type ToolTransport = "rest" | "sse" | "mcp" | "cli" | "internal" | "skill";

export type UnitKind = "currency" | "percent" | "shares" | "days" | "ton" | "quantity" | "text";

/** 请求参数没通过校验（HTTP 422）。 */
export interface ValidationErrorResponse {
  detail: ValidationIssue[];
}

/**
 * 一条参数校验失败。
 * 
 * 这个结构**刻意保持与 FastAPI 默认的 422 响应同形**（`detail` 数组，每项带
 * `loc` / `type` / `msg`），只把 `msg` 换成中文。改形状会让前端里已有的
 * `detail[0].msg` 取值方式失效，而那是所有人都见过的默认约定。
 */
export interface ValidationIssue {
  /** 点号分隔的字段路径，如 'project_id' */
  field: string;
  /** Pydantic 的原始位置串 */
  loc: (string | number)[];
  /** 机读错误码，如 'string_too_short'；前端按它分支 */
  type: string;
  /** 中文说明 */
  msg: string;
  /** 触发问题的原始输入 */
  input?: unknown | null;
}

/** 一个估值参数。**每个参数都必须标记来源**，禁止模型凭记忆填入。 */
export interface ValuationParam {
  param_id: string;
  scenario_id: string;
  year?: number | null;
  name: string;
  value: string;
  source_type: ParamSourceType;
  /** fact_id / comparable_id / 'user:<uid>' / 'assumption:<key>' */
  source_ref: string;
}

/** 一次估值运行。 */
export interface ValuationRun {
  run_id: string;
  project_id: string;
  task_id?: string | null;
  /** 指数→情景的传导来源 */
  diagnosis_run_id?: string | null;
  model?: string;
  currency?: string;
  forecast_years: number;
  base_year: string;
  tax_rate: string;
  net_debt: string;
  net_debt_source: string;
  non_operating_assets?: string | null;
  minority_interest?: string | null;
  lease_liability?: string | null;
  /** EV → 股权价值必需 */
  diluted_shares: string;
  diluted_shares_source: string;
  terminal_method?: string;
  /** 必须显式标注当前处于周期什么位置，不能假装周期不存在 */
  cycle_position?: CyclePosition;
  cycle_position_basis?: string | null;
  /** 周期行业必填，且被引用那次运行必须是已正常化状态；否则 DCF 必须拒绝计算 */
  normalized_basis_id?: string | null;
  /** 默认 reported；只有会计逐笔批准后才允许切到 adjusted */
  ebit_basis?: EbitVariant;
  exit_multiple_check?: string | null;
  implied_exit_multiple?: string | null;
  scenarios?: ValuationScenario[];
  rule_config_version: number;
  created_at: string;
}

/** 一个估值情景。输出永远是区间，不是单一目标价。 */
export interface ValuationScenario {
  scenario_id: string;
  run_id: string;
  /** base / bull / bear */
  scenario: string;
  /** 由诊断指数的映射规则给出，非手填 */
  weight: string;
  enterprise_value?: string | null;
  equity_value?: string | null;
  value_per_share?: string | null;
  param_delta?: Record<string, unknown>;
  /** 引用 diagnosis_component.id，构成「原参数—新参数—触发证据」 */
  trigger_refs?: string[];
  params?: ValuationParam[];
  created_at: string;
}

export type ValueType = "stock" | "flow" | "ratio" | "text";

export type WindowMode = "primary_8y" | "fallback_10y";

