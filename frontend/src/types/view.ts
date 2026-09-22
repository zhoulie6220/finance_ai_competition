/**
 * 工具产出的形状。
 *
 * ⚠ **这些类型不在 `contract.ts` 里，因为它们不是 HTTP 响应模型**——
 * 它们是 `step.succeeded` 事件 `payload.value` 里那一坨，也就是 Skill 和 Tool
 * 自己的返回值。后端那边由 `app/skills/facts.py` 的 `output_schema` 描述。
 *
 * 所以这里有一处**必须人工保持同步**的地方，写清楚免得踩：
 * 后端给 `facts.series` 的 points 加一个字段，这里不加，前端不会报错，
 * 只是那个字段永远显示不出来。**改后端 tool 输出时，顺手看一眼这个文件。**
 *
 * 字段名一律照抄后端，不做「翻译成前端顺口的名字」——
 * 翻译就要维护一张映射表，而映射表迟早和后端分叉，且分叉时静默。
 */

/** 一处年报出处。与后端 `facts.py::_SRC_KEYS` 逐字对应。 */
export interface EvidenceSource {
  /** 在算式里扮演什么角色，例如「减数 · 营业成本」。只有派生值才有。 */
  role?: string
  fact_id?: string | null
  source_file?: string | null
  source_page?: number | null
  source_table?: string | null
  /** 年报那一行的原文。**照抄，不在前端做任何加工。** */
  source_text?: string | null
}

/** `facts.series` 的一个数据点。 */
export interface SeriesPoint extends EvidenceSource {
  period: string
  /** 百万元，字符串形式（后端用 Decimal，结果一律以字符串出入） */
  value: string
  /** 同比。不可比时为 null，理由在 `yoy_note` 里 */
  yoy?: string | null
  yoy_note?: string | null
  yoy_formula?: string
  /** 同一事实在别的年报里被披露成了不同的值（重述）。 */
  restated?: boolean
  restatement_note?: string | null
}

export interface SeriesResult extends EvidenceSource {
  metric_key: string
  label_cn: string
  unit: string
  points: SeriesPoint[]
  refused_years?: string[]
}

/** `facts.margin` 的一个数据点。 */
export interface MarginPoint {
  period: string
  /** 百分数，例如 "5.4544" 表示 5.45%。不可比时为 null。 */
  margin: string | null
  /** 拒绝出数的理由。有值时**不要显示数字**。 */
  note: string | null
  formula?: string
  inputs?: unknown
  gross_is_derived?: boolean
  /** 派生的毛利没有单一出处，出处全在 `sources` 里 */
  source_file?: string | null
  source_page?: number | null
  source_text?: string | null
  sources?: EvidenceSource[]
}

export interface MarginResult {
  label_cn: string
  unit: string
  points: MarginPoint[]
}

/** `facts.coverage` 的产出。 */
export interface CoverageResult {
  project_id: string
  project_name: string
  company_name?: string
  stock_code?: string
  years: string[]
  metrics: number
  facts: number
  /** 工具内部报错时只带这个字段。有它就说明没取到数据。 */
  error?: string
}

// ------------------------------------------------------------------ 形状识别

/**
 * 按**形状**认出这是哪一类产出。
 *
 * 为什么不按步骤序号或步骤名认：序号会随后端加一步而整体错位，
 * 步骤名是中文文案、改一个措辞就认不出来——两种失败都是静默的，
 * 界面上只是那块卡片凭空消失。
 *
 * 形状则是**工具契约本身**（`output_schema` 描述的就是它），
 * 后端要改形状就得先改契约，不会悄悄变。
 */
export function asCoverage(v: Record<string, unknown> | null): CoverageResult | null {
  if (!v) return null
  const r = unwrap(v)
  return Array.isArray(r?.years) && typeof r?.facts === 'number'
    ? (r as unknown as CoverageResult)
    : null
}

export function asSeries(v: Record<string, unknown> | null): SeriesResult | null {
  if (!v) return null
  const r = unwrap(v)
  const points = r?.points as SeriesPoint[] | undefined
  if (!Array.isArray(points) || !points.length) return null
  // 毛利率的点带 `margin` 而没有 `value`——用它把两种序列分开
  if (!('value' in points[0])) return null
  return r as unknown as SeriesResult
}

export function asMargin(v: Record<string, unknown> | null): MarginResult | null {
  if (!v) return null
  const r = unwrap(v)
  const points = r?.points as MarginPoint[] | undefined
  if (!Array.isArray(points) || !points.length) return null
  return 'margin' in points[0] ? (r as unknown as MarginResult) : null
}

/**
 * 拆掉编排器给工具结果套的那一层。
 *
 * `app/agents/orchestrator.py` 把工具产出包成
 * `{result, formula, inputs}`——`result` 才是工具真正的 value。
 * 不拆的话 `v.points` 永远是 undefined，表现为「结果面板一片空白」，
 * 而**不报任何错**。Skill 自己产出的步骤（如「汇总」）没有这一层，
 * 所以这里要判断而不是硬拆。
 */
export function unwrap(v: Record<string, unknown>): Record<string, unknown> | null {
  const inner = v.result
  if (inner && typeof inner === 'object' && !Array.isArray(inner)) {
    return inner as Record<string, unknown>
  }
  return v
}
