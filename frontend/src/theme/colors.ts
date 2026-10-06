/**
 * 全站颜色常量 —— **语义色只有这一个真源**。
 *
 * ⚠ 表现令牌（字号 / 灰阶 / 线面）的真源在 `index.css` 的 `:root`。
 * 语义色在这里，`index.css` 里的 `--ok / --bad / --warn / --mute / --info`
 * 是**镜像**：CSS 读不到 TS 常量，所以只能抄一份，**改一边必须改另一边**。
 *
 * 散在各处写死十六进制值的后果是：有人把涨绿跌红改成红涨绿跌时只改了一半，
 * 页面上同一份数据在不同地方用不同的颜色表达，而且没人会发现。
 *
 * ## 两套红绿，不能合并
 *
 * | | 含义 | 用在哪 |
 * |---|---|---|
 * | `PRICE` **涨跌色** | 数值比上年**增加** = 红 / **减少** = 绿 | 事实网格里那根同比柱 |
 * | `VERDICT` **语义色** | 主张与事实**相悖** = 红 / **支持** = 绿 | 判定、指数、勾稽 |
 *
 * 合并的后果不是"不好看"，是**读错**：判定表里那个红色会被读成"下跌"，
 * 而它说的是"管理层这句话没被财务事实验证"。演示旁白专门讲了这一句。
 */

/**
 * A 股惯例：**红涨绿跌**。与欧美相反，这是刻意的。
 *
 * 用在事实网格每格下面那根闰比柱上：红 = 比上年增加，绿 = 减少。
 * 柱子的数据（增长率与方向）是**后端算好的**（`FactCellView.change_dir`），
 * 前端只负责上色——见 `pages/FactTable.tsx`。
 */
export const PRICE = {
  up: '#cf1322', // 比上年增加 → 红
  down: '#389e0d', // 比上年减少 → 绿
  flat: '#b0b0b0', // 基本持平（±1% 内）
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
  neutral: { color: '#767676', label: '无明显变化' },
  contradicted: { color: '#cf1322', label: '相悖' },
  needs_review: { color: '#d46b08', label: '待核查' },
  unverifiable: { color: '#597ef7', label: '不可验证' },
  incomparable: { color: '#767676', label: '不可比' },
} as const

/** 勾稽校验的结论色。 */
export const CHECK = {
  passed: { color: '#389e0d', label: '通过' },
  failed: { color: '#cf1322', label: '不平' },
  skipped_missing_data: { color: '#767676', label: '缺数据' },
  skipped_incomparable: { color: '#767676', label: '不可比' },
} as const

/** 严重度。error 是报表本身不平；warn 多半是字典缺字段。 */
export const SEVERITY = {
  error: { color: '#cf1322', label: '报表不平' },
  warn: { color: '#d46b08', label: '存疑' },
  info: { color: '#767676', label: '提示' },
} as const

/** 网格里四种格子状态。必须视觉可分且绝不混淆。 */
export const CELL = {
  valued: { color: '#1f1f1f', label: '有值' },
  needs_review: { color: '#d46b08', label: '待复核' },
  not_found: { color: '#b0b0b0', label: '未找到' },
  incomparable: { color: '#767676', label: '不可比' },
} as const
