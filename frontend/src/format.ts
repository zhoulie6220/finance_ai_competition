/**
 * 展示层格式化。
 *
 * ⚠ **这里只做格式化，不做算术。** 加千位分隔符是在排版一个字符串，
 * 不是计算；而 `x / 100`、`x * 100`、算同比这类都是业务计算，
 * 必须由后端的确定性引擎做——赛事的硬要求是「计算可复算」，
 * 浏览器里的算术评审没法核。
 */

const NULL_TEXT = '—'

/** 给数字字符串加千位分隔符。**不改变数值**，只插入逗号。 */
export function groupDigits(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return NULL_TEXT
  const negative = value.startsWith('-')
  const body = negative ? value.slice(1) : value
  const [intPart, fracPart] = body.split('.')
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const text = fracPart ? `${grouped}.${fracPart}` : grouped
  return negative ? `-${text}` : text
}

/** 金额展示：千位分隔 + 单位。单位来自数据本身，不在这里换算。 */
export function formatMoney(
  value: string | null | undefined,
  unit?: string | null,
): string {
  if (value === null || value === undefined || value === '') return NULL_TEXT
  return unit ? `${groupDigits(value)} ${unit}` : groupDigits(value)
}

/**
 * 比值展示成百分比。
 *
 * 这里确实做了一次算术（×100），但它**只是把同一张脸换成另一种写法**，
 * 不产生新信息：0.0742 和 7.42% 是同一个数。凡是会产生新信息的计算
 * （同比、差额、比率）都由后端算好再传过来。
 */
export function formatRatio(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return NULL_TEXT
  const num = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(num)) return NULL_TEXT
  return `${(num * 100).toFixed(2)}%`
}

/** 置信度：0–1 的小数，展示成百分比。同样是换写法，不是计算。 */
export const formatConfidence = formatRatio

/** 页码展示：印刷页码与物理页序不一致时把两个都写出来。 */
export function formatPage(
  pageNo: number | null | undefined,
  printedPageNo?: string | null,
): string {
  if (pageNo === null || pageNo === undefined) return NULL_TEXT
  if (printedPageNo && printedPageNo !== String(pageNo)) {
    return `第 ${pageNo} 页（印刷页 ${printedPageNo}）`
  }
  return `第 ${pageNo} 页`
}

/** 文件名去掉路径，只留最后一段。 */
export function basename(path: string | null | undefined): string {
  if (!path) return NULL_TEXT
  return path.replace(/\\/g, '/').split('/').pop() ?? path
}

/** 口径的中文名。 */
export function scopeLabel(scope: string): string {
  return scope === 'parent' ? '母公司口径' : '合并口径'
}

/** 报表的中文名。 */
export const STATEMENT_LABELS: Record<string, string> = {
  income: '利润表',
  balance: '资产负债表',
  cashflow: '现金流量表',
  indicator: '主要指标',
  industry: '行业指标',
  disclosure: '披露事项',
}

export function statementLabel(statement: string): string {
  return STATEMENT_LABELS[statement] ?? statement
}
