/**
 * 接口响应的类型。
 *
 * **手写而不是从 `contract.json` 生成**，理由有两条：
 *   1. 那个文件是 111KB 的 JSON Schema，`resolveJsonModule` 一开，`tsc` 会给
 *      每个键推一个字面量类型，`tsc -b` 明显变慢；而它给的是 **schema 形状**
 *      （properties / anyOf / $ref），不是接口实际返回的形状，用起来还要再转一层。
 *   2. 这里定义的几个读模型（网格、证据、校验结论）在后端是**聚合出来的**，
 *      没有对应的 Pydantic 模型，生成也生成不出来。
 *
 * 枚举值刻意用字面量联合类型而不是 TS enum —— tsconfig 开了
 * `erasableSyntaxOnly`（禁 enum / namespace），而且联合类型在这个场景下更好用：
 * 少写一个分支编译器会直接报错。
 *
 * ⚠ 金额一律是 **string**。后端刻意这么序列化的：JSON number 是 IEEE 754 双精度，
 * 几十亿的金额往返会掉精度（12345678901.23 → 12345678901.229998）且不报错。
 * 前端只做格式化，**不做任何算术**。
 */

// ---------------------------------------------------------------- 枚举

export type FactStatus = 'validated' | 'needs_review' | 'not_found' | 'rejected'

export type Verdict =
  | 'supported'
  | 'neutral'
  | 'contradicted'
  | 'needs_review'
  | 'unverifiable'
  | 'incomparable'

export type CheckStatus =
  | 'passed'
  | 'failed'
  | 'skipped_missing_data'
  | 'skipped_incomparable'

export type Severity = 'info' | 'warn' | 'error'

export type LlmMode = 'live' | 'replay'

// ---------------------------------------------------------------- 健康检查

export interface Health {
  status: 'ok' | 'empty'
  db_ok: boolean
  /** 库为空时页面上要显示的原因与处置办法。为空表示库是好的。 */
  db_hint: string | null
  counts: Record<string, number>
  llm_mode: LlmMode
  llm_configured: boolean
  llm_description: string
  offline_mode: boolean
  rule_config_version: number
}

// ---------------------------------------------------------------- 项目

export interface ProjectFile {
  file_id: string
  role: string
  period: string
  file_name: string
  page_count: number | null
  parse_status: string
}

export interface Project {
  project_id: string
  name: string
  company_name: string
  stock_code: string
  industry: string
  base_scope: string
  fiscal_years: string[]
  file_count?: number
  fact_count?: number
  mdna_count?: number
  files?: ProjectFile[]
}

// ---------------------------------------------------------------- 财务事实网格

export interface FactCell {
  fact_id: string | null
  /** 字符串。见文件头的说明。 */
  value: string | null
  unit: string | null
  raw_unit: string | null
  status: FactStatus
  comparable: boolean
  incomparable_reason: string | null
  source_file: string | null
  source_page: number | null
  confidence: number | null
  /**
   * **后端算好的**同比增长率（相对上一列）。
   *
   * ⚠ 前端只拿它画柱子，**不自己算**。项目铁律：同比、比率、估值一律由
   * `backend/app/engine/` 算好返回——浏览器里算的东西没法审计，
   * 而「计算可复算、过程可追溯」是比赛的硬要求。
   */
  change: string | null
  /**
   * `up` 比上年增加（红）/ `down` 减少（绿）/ `flat` 基本持平。
   *
   * ⚠ 这是**涨跌色**，与判定用的语义色是两套体系，见 `theme/colors.ts`。
   */
  change_dir: 'up' | 'down' | 'flat' | null
  /** 算不出同比的原因。**第一个年度没有上期，这里也是 null**——那不是拒绝。 */
  change_refused: string | null
  change_formula: string | null
  change_inputs: Record<string, string>

  // ---- 派生格专用 ----
  // ⚠ **派生值不是年报原文**，它的 fact_id / source_page 都是 null，
  //   出处是 derived_sources 里那几行。前端据此把这类格子画得和
  //   「从年报抄来的」明显不同——不区分的话，看的人会以为
  //   「毛利率 5.45%」也是从年报某一页抄下来的。
  derived: boolean
  derived_formula: string | null
  derived_inputs: Record<string, string>
  derived_sources: DerivedSource[]
  /** 算不出来的原因。有 derived=true 但值为空时，这里必有理由 */
  derived_refused: string | null
}

/** 派生值的一个输入行。点开派生格时逐行显示，每行还能再点回它自己的年报页。 */
export interface DerivedSource {
  metric_key: string
  label_cn: string
  period: string
  /** 点回原文靠它。为空说明这一行自己也定位不到出处 */
  fact_id: string | null
  value: string | null
  source_page: number | null
}

export interface FactGridRow {
  metric_key: string
  label_cn: string
  statement: string
  unit_kind: string
  /** 期间 → 格子。**每个年度都有一格**，没数据的是 not_found 而不是缺行。 */
  cells: Record<string, FactCell>
  /**
   * 有值的年度数。**后端数出来的**。
   *
   * 91 个指标里有近一半整列是空的（还没采集）。下拉里不标出来的话，
   * 点进走势图是一张空图，**看起来像系统坏了**——而实际是「这一项没采到」。
   */
  filled: number
}

export interface FactGrid {
  project_id: string
  company_name: string
  scope: string
  periods: string[]
  metrics: FactGridRow[]
}

// ---------------------------------------------------------------- 事实与原文

export interface FactDetail {
  fact_id: string
  project_id: string
  company_id: string
  is_primary: boolean
  metric: string
  value: string | null
  unit: string | null
  period: string
  scope: string
  source_file: string
  source_page: number
  source_text: string
  confidence: number
  status: FactStatus
  period_kind: string
  value_raw: string | null
  raw_unit: string | null
  unit_factor: string | null
  source_table: string | null
  source_row_label: string | null
  source_printed_page: string | null
  bbox: string | null
  extractor: string
  comparable: boolean
  incomparable_reason: string | null
  restated: boolean
  file_name: string | null
  file_period: string | null
  sign_basis: string | null
  mapped_from: string | null
}

export interface PageDetail {
  page_id: string
  file_id: string
  file_name: string
  period: string
  role: string
  page_no: number
  printed_page_no: string | null
  text: string | null
  text_source: 'native' | 'ocr'
  has_table: boolean
  /** 前后页由**后端**给出 page_id。前端不自己拼 id——那种隐式约定一旦
   *  和后端的 id 生成规则分叉，表现为「翻页点了没反应」，且不报错。 */
  prev_page_id: string | null
  next_page_id: string | null
  first_page_no: number | null
  last_page_no: number | null
}

// ---------------------------------------------------------------- 勾稽校验

export interface CheckSummary {
  total: number
  evaluable: number
  passed: number
  failed: number
  /** 报表本身不平。几可断定解析错了。 */
  hard_failed: number
  /** 存疑，多为字典缺字段。**不是**报表有问题。 */
  soft_failed: number
  skipped_missing_data: number
  skipped_incomparable: number
  sheet_ok: boolean
  coverage_line: string
}

export interface CheckResult {
  rule_key: string
  period: string
  scope: string
  status: CheckStatus
  severity: Severity
  lhs: string | null
  rhs: string | null
  diff: string | null
  tolerance: string | null
  formula: string
  inputs: string[]
  message: string
  suggestion: string | null
}

export interface ChecksResponse {
  project_id: string
  scope: string
  method_version: string
  summary: CheckSummary
  results: CheckResult[]
  /** 取数阶段的问题（值解析不出来、同一指标多行等）。空数组表示干净。 */
  warnings: string[]
}

// ---------------------------------------------------------------- 叙事一致性

export interface Claim {
  claim_id: string
  claim_text: string
  claim_type: string
  direction: string
  period_norm: string | null
  period_expr: string | null
  magnitude_text: string | null
  magnitude_value: string | null
  magnitude_unit: string | null
  verifiable: number
  background_only: number
  confidence: number
  status: string
  source_page: number
  primary_metric: string | null
  /** 该主题**被禁止的简化推断**。随主张一起展示，供人工复核对照。 */
  forbidden_simplifications: string[]
}

export interface ClaimStats {
  total: number
  verifiable: number
  background_only: number
  validated: number
  by_type: Record<string, number>
}

/** 一种抽取法的概况。用来做「规则法 vs LLM」的并排对照。 */
export interface ExtractorSummary {
  /** `rule:claim_v1` 或 `llm:deepseek-chat@<prompt_hash>` */
  extractor: string
  total: number
  verifiable: number
  validated: number
  theme_count: number
}

export interface ClaimsResponse {
  project_id: string
  stats: ClaimStats
  /** **验收标准要求的「能和规则法对照」就落在这里。** */
  by_extractor: ExtractorSummary[]
  claims: Claim[]
}

export interface ClaimMatch {
  match_id: string
  claim_id: string
  metric_key: string
  verdict: Verdict
  reason: string
  confidence: number
  claim_period: string
  fact_period: string
  direction_claim: string | null
  direction_actual: string | null
  magnitude_target: string | null
  magnitude_actual: string | null
  relative_deviation: string | null
  formula: string | null
  inputs: string | null
  /**
   * 绝对量目标的换算留痕（会计口径 8-1）。
   *
   * 目标原样写着「亿元」，而财务事实库存的是「百万元」——这一步换算
   * 必须显示出来。只给一个换算过的数，用户看不出它从哪来，
   * 而这正是本系统「计算可复算」的卖点所在。
   */
  target_unit: string | null
  target_millions: string | null
  unit_factor: string | null
  /**
   * 「原始计划偏差」= 实际 − 换算后的目标（百万元）。
   *
   * ⚠ 会计口径 8-3 第 4 条：**只作展示，不进 H 的支持/相悖判定**。
   * 所以它非空时 `verdict` 一定是 `needs_review`。
   */
  plan_variance: string | null
  /**
   * 按 8-2 的方向算出的参考结论（「实际高于计划，属未达成」这类）。
   * ⚠ 是**文字参考，不是判定**——判定看 `verdict`。
   */
  plan_reference: string | null
  claim_text: string
  claim_type: string
  source_page: number
  verifiable: number
  background_only: number
}

export interface MatchesResponse {
  project_id: string
  counts: Record<string, number>
  /** 没跑过匹配时给出的说明。有数据时为 null。 */
  hint: string | null
  matches: ClaimMatch[]
}

export interface IndexComponents {
  history: string | null
  current: string | null
  risk: string | null
  template: string | null
  quality: string | null
}

/** `GET /narrative/index/components/{key}` 的一条构成记录。
 *  形状统一——主张 / 风险项 / 模板判定 / 质量检查都走这一套。 */
export interface ComponentEvidenceRow {
  kind: 'claim' | 'risk' | 'template' | 'quality'
  /** 记录本身：主张原文（截断到 160 字）、风险项名、检查项名 */
  label: string
  detail: string
  /** 对分项的贡献：H/C 是 s（+1/0/-1），R/P/Q 是结论或触发态 */
  contribution: string
  period: string | null
  report_period: string | null
  /** 主判据的中文名 */
  metric_label: string | null
  source_page: number | null
  /** 人工录入的项指向会计抄录的原文片段 */
  source_text: string | null
  reviewer: string | null
  claim_id: string | null
  fact_id: string | null
  formula: string | null
  /** 代入公式的数。**后端原样给，前端不重算** */
  inputs: string[]
  verdict: string | null
}

export interface ComponentDetail {
  project_id: string
  key: string
  label_cn: string
  rows: ComponentEvidenceRow[]
  total: number
  /** 截断了要显式说——不说的话页面像「一共就这么多条」，而分母是全量 */
  truncated: boolean
  note: string | null
}

export interface NarrativeIndex {
  project_id: string
  status: 'scored' | 'insufficient_evidence'
  grade: 'high' | 'medium' | 'low' | 'insufficient_evidence'
  /** 字符串。闸门不过时是 null——**绝不用 0 或 50 代替**。 */
  score: string | null
  components: IndexComponents
  coverage: string | null
  counts: {
    n: number
    N: number
    history_observations: number
    current_observations: number
    skipped_no_period: number
    skipped_no_fact: number
  }
  /**
   * 闸门四条件的逐条结果。**出分时四条全为 passed**。
   *
   * 结构化出来是为了页面上一眼看出「四项里过了三项、只差哪一项」——
   * 只给一句 `insufficient_reason` 的话，读的人分不出「差一张表」和
   * 「差得远」，而这两件事该做的事完全不同。
   * ⚠ 阈值与判定都在后端，前端只渲染 `passed`，不重算。
   */
  gate: {
    key: string
    /** 如「覆盖率 ≥ 0.60」，阈值已经拼在里面了 */
    label_cn: string
    passed: boolean
    /** 后端给的实测值，如「49/67 = 0.731343」 */
    detail: string
  }[]
  /**
   * Q2 逐对比较的进度，如「8/9 组比较已完成，1 组待核查」。
   *
   * ⚠ **只作展示，不参与闸门判定。** 会计 2026-10-06 答复原话：
   * 「页面可以另外展示『8/9组比较已完成，1组因原始披露缺失待核查』，
   * 但不能直接剔除分母后作为正式 Q 值」。
   * 所以这里显示进度，闸门那边 Q 仍然是不完整、指数照样不出分——
   * 一个是「做到哪一步了」，一个是「能不能出正式分数」。
   */
  q2_progress: {
    done: number
    total: number
    /** 后端拼好的一句话，前端不重算 */
    line: string
    pairs: {
      prior: string
      current: string
      status: string
      done: boolean
      reason: string | null
    }[]
  } | null
  insufficient_reason: string | null
  /**
   * 出分时**必须跟着分数一起说**的话。目前只有一条：H 的观测太少、
   * 稳健性低（H 权重 20 分，换掉那一条观测总分就动 20 分）。
   *
   * ⚠ 会计口径明确要求「不能只报分数」——那个分数和另一个同分项目
   * 在页面上长得一模一样，看的人分不出哪个脆。
   */
  caveats: string[]
  formula: string
  conclusion_boundary: string
  /** 指数 → 估值情景的传导。**没有目标价字段，也不该有。** */
  scenarios: {
    /** 不足以出分时是 null——给一组「差不多的权重」会让闸门形同虚设。 */
    weights: { base: string; optimistic: string; stress: string } | null
    revenue_growth_ref: string | null
    requires_human_confirmation: boolean
    changes_valuation: boolean
    /** 每次传导都附带的约束说明，复核的人要看得见边界在哪。 */
    notes: string[]
  }
  action: string
  valuation_action: string
  user_hint: string
  method_version: string
}

// ---------------------------------------------------------------- 任务事件

export interface TaskEvent {
  step_id: string
  seq: number
  name: string
  status: string
  tool_name: string | null
  started_at: string | null
  finished_at: string | null
  error: string | null
}
