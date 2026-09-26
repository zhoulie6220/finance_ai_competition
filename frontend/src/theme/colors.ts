/**
 * 全站颜色常量。
 *
 * **所有颜色只在这里定义一次。** 散在各处写死十六进制值的后果是：
 * 有人把涨绿跌红改成红涨绿跌时只改了一半，页面上同一份数据在不同地方
 * 用不同的颜色表达，而且没人会发现。
 */

/** A 股惯例：红涨绿跌。与欧美相反，这是刻意的。 */
export const PRICE = {
  up: '#d4380d', // 涨
  down: '#389e0d', // 跌
  flat: '#8c8c8c',
} as const

/**
 * 判定结果用**语义色**，不用涨跌色。
 *
 * 支持=绿、相悖=红、无明显变化=灰、待核查=橙、不可验证=蓝灰。
 * 这跟红涨绿跌是两套体系：涨跌描述的是**数值的方向**，判定描述的是
 * 「主张与事实是否一致」。混用会让「相悖」这个红色被读成「下跌」——
 * 一个评委一眼能看出的理解错误。
 */
export const VERDICT = {
  supported: { color: '#389e0d', label: '支持' },
  neutral: { color: '#8c8c8c', label: '无明显变化' },
  contradicted: { color: '#cf1322', label: '相悖' },
  needs_review: { color: '#d46b08', label: '待核查' },
  unverifiable: { color: '#597ef7', label: '不可验证' },
  incomparable: { color: '#8c8c8c', label: '不可比' },
} as const

/** 勾稽校验的结论色。 */
export const CHECK = {
  passed: { color: '#389e0d', label: '通过' },
  failed: { color: '#cf1322', label: '不平' },
  skipped_missing_data: { color: '#8c8c8c', label: '缺数据' },
  skipped_incomparable: { color: '#8c8c8c', label: '不可比' },
} as const

/** 严重度。error 是报表本身不平；warn 多半是字典缺字段。 */
export const SEVERITY = {
  error: { color: '#cf1322', label: '报表不平' },
  warn: { color: '#d46b08', label: '存疑' },
  info: { color: '#8c8c8c', label: '提示' },
} as const

/** 网格里四种格子状态。必须视觉可分且绝不混淆。 */
export const CELL = {
  valued: { color: '#1f1f1f', label: '有值' },
  needs_review: { color: '#d46b08', label: '待复核' },
  not_found: { color: '#bfbfbf', label: '未找到' },
  incomparable: { color: '#8c8c8c', label: '不可比' },
} as const
