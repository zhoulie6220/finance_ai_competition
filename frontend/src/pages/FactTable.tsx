import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'

import { useApi } from '../api/client'
import type { FactCell, FactGrid, FactGridRow } from '../api/types'
import AsyncBoundary from '../components/AsyncBoundary'
import EvidenceModal from '../components/EvidenceModal'
import { groupDigits, statementLabel } from '../format'

/**
 * 财务事实表：指标 × 年度网格，点任意格子跳回年报原文。
 *
 * 这是「点结论回到计算过程」最直观的展示，也是演示动线的主干。
 *
 * **四种格子状态必须视觉可分且绝不混淆**（见 theme/colors.ts 的 CELL）：
 *
 *   有值      蓝色带下划线的数字
 *   未找到    灰色「—」，提示语明说「未在年报中定位到，**不是 0**」
 *   待复核    琥珀色
 *   不可比    灰色 + 斜纹 + 原因
 *
 * 「不是 0」那句话不是装饰。硬规则二是「缺失即无行，绝不插补 0」——
 * 页面上一格空白，看的人默认会读成 0，然后毛利率变成 -100% 之类的怪数
 * 就没人知道从哪来的了。把规则写在提示里，是让它变成可见的。
 *
 * ## 每格下面那根柱子
 *
 * 是**比上年**的同比（红增绿减，A 股惯例）。⚠ 数值是**后端算好的**
 * （`FactCell.change` / `change_dir`），前端只上色——同比属于业务计算，
 * 在浏览器里算违反项目铁律。算不出时后端给 `change_refused`，这一格就不画柱子。
 */
export default function FactTable() {
  const { projectId } = useParams<{ projectId: string }>()
  const [factId, setFactId] = useState<string | null>(null)
  const [year, setYear] = useState<string | null>(null)

  const grid = useApi<FactGrid>(
    projectId ? `/projects/${projectId}/fact-grid` : null,
  )

  const grouped = useMemo(() => groupByStatement(grid.data), [grid.data])

  return (
    <div className="page">
      <header className="page-head">
        <h2>
          财务事实表
          {grid.data && (
            <span className="subtitle">
              {grid.data.company_name}（{grid.data.periods[0]}–
              {grid.data.periods[grid.data.periods.length - 1]}）
            </span>
          )}
        </h2>
        <p className="page-note">
          点击任意有值的格子回到年报原文。灰色「—」表示未在年报中定位到，
          <strong>不是 0</strong>。数字下面的柱子是<b>比上年</b>：红增绿减。
        </p>
      </header>

      <AsyncBoundary
        loading={grid.loading}
        error={grid.error}
        onRetry={grid.reload}
      >
        {grid.data && grid.data.metrics.length === 0 && (
          <div className="async-state async-empty">
            <div className="async-detail">
              这个项目还没有财务事实。若刚重建过库，需要按顺序补跑：
              <code>python scripts/parse_reports.py --source var/samples</code>
              <code>python scripts/parse_mdna.py</code>
            </div>
          </div>
        )}

        {grid.data && grid.data.metrics.length > 0 && (
          <div className="card">
            {/* 年度 pill：点一下高亮整列。91 行 × 10 列的一张表，
                说「看 2024 这一列」时，用手在屏幕上找很难看。 */}
            <div className="pill-row">
              <span className="pill-label">年度</span>
              <button
                type="button"
                className={year === null ? 'pill active' : 'pill'}
                onClick={() => setYear(null)}
              >
                全部
              </button>
              {grid.data.periods.map((p) => (
                <button
                  key={p}
                  type="button"
                  className={year === p ? 'pill active' : 'pill'}
                  onClick={() => setYear(p)}
                >
                  {p}
                </button>
              ))}
            </div>

            <div className="grid-wrap">
              <table className="fact-grid">
                <thead>
                  <tr>
                    <th className="sticky-col sticky-head">指标</th>
                    {grid.data.periods.map((p) => (
                      <th
                        key={p}
                        className={year === p ? 'sticky-head num col-on' : 'sticky-head num'}
                      >
                        {p}
                      </th>
                    ))}
                  </tr>
                </thead>
                {grouped.map(([statement, rows]) => (
                  <tbody key={statement}>
                    <tr className="group-row">
                      <th
                        className="sticky-col"
                        colSpan={grid.data!.periods.length + 1}
                      >
                        {statementLabel(statement)}
                      </th>
                    </tr>
                    {rows.map((row) => (
                      <tr key={row.metric_key}>
                        <th className="sticky-col" title={row.metric_key}>
                          {row.label_cn}
                        </th>
                        {grid.data!.periods.map((period) => (
                          <Cell
                            key={period}
                            cell={row.cells[period]}
                            on={year === period}
                            onOpen={setFactId}
                          />
                        ))}
                      </tr>
                    ))}
                  </tbody>
                ))}
              </table>
            </div>
          </div>
        )}
      </AsyncBoundary>

      <EvidenceModal factId={factId} onClose={() => setFactId(null)} />
    </div>
  )
}

// ---------------------------------------------------------------- 格子

function Cell({
  cell,
  on,
  onOpen,
}: {
  cell: FactCell | undefined
  on: boolean
  onOpen: (factId: string) => void
}) {
  const cls = on ? 'num col-on' : 'num'

  if (!cell) {
    // 视图是完整的指标 × 年度笛卡尔积，理论上到不了这里。
    // 真空了说明视图被换过——如实显示，不要静默留白。
    return <td className={`${cls} cell-missing`} title="这一格没有从视图返回">?</td>
  }

  if (!cell.comparable) {
    return (
      <td
        className={`${cls} cell-incomparable`}
        title={`不可比：${cell.incomparable_reason ?? '未注明原因'}（不进指数、不进中枢）`}
      >
        {cell.value ? groupDigits(cell.value) : '不可比'}
      </td>
    )
  }

  if (cell.status === 'not_found' || cell.value === null) {
    return (
      <td className={`${cls} cell-notfound`} title="未在年报中定位到，不是 0">
        —
      </td>
    )
  }

  if (cell.status !== 'validated') {
    return (
      <td
        className={`${cls} cell-review`}
        title={`状态：${cell.status}（尚未通过核验，不进指数与估值）`}
      >
        <span className="cell-stack">
          <span>{groupDigits(cell.value)}</span>
          <Yoy cell={cell} />
        </span>
      </td>
    )
  }

  return (
    <td className={cls}>
      <span className="cell-stack">
        <button
          type="button"
          className="cell-button"
          onClick={() => cell.fact_id && onOpen(cell.fact_id)}
          disabled={!cell.fact_id}
          title={`来源：${cell.source_file ?? ''} 第 ${cell.source_page ?? '?'} 页`}
        >
          {groupDigits(cell.value)}
        </button>
        <Yoy cell={cell} />
      </span>
    </td>
  )
}

/**
 * 同比柱：**红增绿减**（A 股惯例）。
 *
 * ⚠ 这一根和判定表里的红绿**不是一个意思**：这里的红是「数值比上年涨了」，
 * 那里的红是「主张与事实相悖」。两套体系，见 `theme/colors.ts` 的说明。
 *
 * 条长在 **±50% 处封顶**。不是随手取的数：净利润同比动辄 ±300%，
 * 不封顶的话其余所有柱子都挤成看不见的一条。封顶之后 +300% 和 +50%
 * 一样长——**具体数值在悬停提示里**。
 *
 * ⚠ 前端在这里只做**排版**：条长由后端给的 `cell.change` 取绝对值缩放而来，
 * **不显示成一个数字**。悬停提示里的算式与代入的数也全是后端给的
 * （`change_formula` / `change_inputs`）。一旦把比例打成文字，
 * 它就成了「前端算出来、还会被评审引用」的新数字。
 */
function Yoy({ cell }: { cell: FactCell }) {
  if (!cell.change || !cell.change_dir) return null

  const dir = cell.change_dir
  const ratio = Math.abs(Number(cell.change))
  // flat 是「基本持平」，给一段固定的短灰条——按比例画的话它只有 1px，
  // 和「没有这一格」长得一样。
  const width = dir === 'flat' ? 5 : Math.max(2, Math.min(100, ratio * 200))

  const head =
    dir === 'flat'
      ? `与上年基本持平（${cell.change}）`
      : `比上年${dir === 'up' ? '增加' : '减少'} ${(ratio * 100).toFixed(2)}%`

  const title =
    `${head}\n${cell.change_formula ?? ''}\n` +
    Object.entries(cell.change_inputs)
      .map(([k, v]) => `${k} = ${v}`)
      .join('\n') +
    (ratio * 200 > 100 ? '\n（柱子按 ±50% 封顶，实际幅度更大）' : '')

  return (
    <span className="yoy" title={title}>
      <i className={dir} style={{ width: `${width}%` }} />
    </span>
  )
}

// ---------------------------------------------------------------- 分组

function groupByStatement(grid: FactGrid | null): [string, FactGridRow[]][] {
  if (!grid) return []
  const buckets = new Map<string, FactGridRow[]>()
  for (const row of grid.metrics) {
    const list = buckets.get(row.statement)
    if (list) list.push(row)
    else buckets.set(row.statement, [row])
  }
  // 按报表分组保持稳定顺序；组内按 label 排，和领域习惯一致
  const order = ['income', 'balance', 'cashflow', 'indicator', 'industry', 'disclosure']
  return [...buckets.entries()]
    .sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]))
    .map(([statement, rows]) => [
      statement,
      rows.slice().sort((a, b) => a.label_cn.localeCompare(b.label_cn, 'zh')),
    ])
}
