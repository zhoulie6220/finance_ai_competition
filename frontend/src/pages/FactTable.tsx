import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'

import { useApi } from '../api/client'
import type { FactCell, FactGrid, FactGridRow } from '../api/types'
import AsyncBoundary from '../components/AsyncBoundary'
import DerivedModal from '../components/DerivedModal'
import EvidenceModal from '../components/EvidenceModal'
import { formatRatio, groupDigits, statementLabel } from '../format'

/**
 * 财务事实总表：91 个指标 × 10 个年度的全量网格。
 *
 * **指标是在左边选的**（侧栏那个下拉），这一页只负责显示总表。
 * 看单个指标的走势去 `/facts/{metric_key}`，也是从左边点进来的。
 *
 * 点任意一个有值的格子回到年报原文——这是「点结论回到计算过程」最直观的
 * 展示，也是演示动线的主干。
 *
 * 四种格子状态必须视觉可分且绝不混淆：有值 / 未找到（**不是 0**）/
 * 待复核 / 不可比。「不是 0」那句话不是装饰：填成 0 看起来最省事，
 * 但毛利率会因此变成负百分之百，而没有任何地方会提示。
 */
export default function FactTable() {
  const { projectId } = useParams<{ projectId: string }>()
  const [factId, setFactId] = useState<string | null>(null)
  const [year, setYear] = useState<string | null>(null)
  // 派生格的「怎么算出来的」。与证据抽屉是**两个**弹窗：
  // 前者给算式与参与计算的那几行，后者给某一行的年报原文。
  const [derived, setDerived] = useState<{
    label: string
    period: string
    cell: FactCell
  } | null>(null)

  const grid = useApi<FactGrid>(
    projectId ? `/projects/${projectId}/fact-grid` : null,
  )

  const grouped = useMemo(() => groupByStatement(grid.data), [grid.data])

  return (
    <div className="page">
      <header className="page-head">
        <h2>
          财务事实总表
          {grid.data && (
            <span className="subtitle">
              {grid.data.company_name}（{grid.data.periods[0]}–
              {grid.data.periods[grid.data.periods.length - 1]}）
            </span>
          )}
        </h2>
        <p className="page-note">
          点任意一个有值的格子回到年报原文。灰色「—」表示未在年报中定位到，
          <strong>不是 0</strong>。数字下面的柱子是<b>比上年</b>，红增绿减。
          看单个指标的走势，从左边选。
        </p>
      </header>

      <AsyncBoundary loading={grid.loading} error={grid.error} onRetry={grid.reload}>
        {grid.data && grid.data.metrics.length === 0 && (
          <div className="async-state async-empty">
<div className="async-detail">这家公司还没有可用的财务事实。</div>
          </div>
        )}

        {grid.data && grid.data.metrics.length > 0 && (
          <section className="card">
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
                        className={
                          year === p ? 'sticky-head num col-on' : 'sticky-head num'
                        }
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
                    {rows.map((r) => (
                      <tr key={r.metric_key}>
                        <th className="sticky-col" title={r.metric_key}>
                          {r.label_cn}
                        </th>
                        {grid.data!.periods.map((period) => (
                          <Cell
                            key={period}
                            label={r.label_cn}
                            period={period}
                            cell={r.cells[period]}
                            on={year === period}
                            onOpen={setFactId}
                            onOpenDerived={setDerived}
                          />
                        ))}
                      </tr>
                    ))}
                  </tbody>
                ))}
              </table>
            </div>
          </section>
        )}
      </AsyncBoundary>

      <EvidenceModal factId={factId} onClose={() => setFactId(null)} />
      {/* 派生格里点「参与计算的记录」那一行 → 关掉这个、开证据抽屉。
          两个弹窗**不叠着开**：叠着的话返回时不知道该退回哪一层。 */}
      {derived && (
        <DerivedModal
          metricLabel={derived.label}
          period={derived.period}
          cell={derived.cell}
          onClose={() => setDerived(null)}
          onOpenFact={(id) => {
            setDerived(null)
            setFactId(id)
          }}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 格子

function Cell({
  cell,
  label,
  period,
  on,
  onOpen,
  onOpenDerived,
}: {
  cell: FactCell | undefined
  label: string
  period: string
  on: boolean
  onOpen: (factId: string) => void
  onOpenDerived: (d: { label: string; period: string; cell: FactCell }) => void
}) {
  const cls = on ? 'num col-on' : 'num'

  if (!cell) {
    // 视图是完整的指标 × 年度笛卡尔积，理论上到不了这里。
    return <td className={`${cls} cell-missing`} title="这一格没有从视图返回">?</td>
  }

  // ---- 派生格 -----------------------------------------------------------
  //
  // ⚠ **派生格必须一眼看得出和别的格子不同。** 它的值不是从年报抄的，
  //   出处是另外几行；画成一样的话，看的人会以为
  //   「毛利率 5.45%」也能点回年报的某一页——而那一页不存在。
  //   所以：左上角一个小三角 + 点击打开的是算式面板而不是原文页。
  if (cell.derived) {
    const has = cell.value !== null
    return (
      <td className={`${cls} ${has ? 'cell-derived' : 'cell-derived-empty'}`}>
        <span className="cell-stack">
          {has ? (
            <button
              type="button"
              className="cell-button"
              onClick={() => onOpenDerived({ label, period, cell })}
              title={`派生值，非年报原文。点开看算式与参与计算的记录`}
            >
              {groupDigits(cell.value as string)}
            </button>
          ) : (
            // 算不出来也**可点**——理由（缺哪个字段、口径未定）比那个「—」有用得多。
            // 不可点的话，这一格和「年报里没有这一项」就完全分不出来了。
            <button
              type="button"
              className="cell-button muted"
              onClick={() => onOpenDerived({ label, period, cell })}
              title="派生值，当前数据算不出来。点开看缺什么"
            >
              —
            </button>
          )}
          {has && <Yoy cell={cell} />}
        </span>
      </td>
    )
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

  const stack = (
    <span className="cell-stack">
      <span>{cell.value && groupDigits(cell.value)}</span>
      <Yoy cell={cell} />
    </span>
  )

  if (cell.status !== 'validated') {
    return (
      <td
        className={`${cls} cell-review`}
        title={`状态：${cell.status}（尚未通过核验，不进指数与估值）`}
      >
        {stack}
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
          {cell.value && groupDigits(cell.value)}
        </button>
        <Yoy cell={cell} />
      </span>
    </td>
  )
}

/**
 * 网格里那根小同比柱：红增绿减。
 *
 * 条长在 **±50% 处封顶**——净利润同比动辄 ±300%，不封顶的话其余所有柱子
 * 都挤成看不见的一条。封顶之后 +300% 和 +50% 一样长，**具体数值在悬停里**。
 *
 * ⚠ 前端只做**排版**：条长由后端给的 `change` 取绝对值缩放而来，
 * **不显示成一个数字**。悬停里的算式与代入的数也全是后端给的。
 */
function Yoy({ cell }: { cell: FactCell }) {
  if (!cell.change || !cell.change_dir) return null

  const dir = cell.change_dir
  const ratio = Math.abs(Number(cell.change))
  // flat 给一段固定的短灰条——按比例画只有 1px，和「没有这一格」长得一样
  const width = dir === 'flat' ? 5 : Math.max(2, Math.min(100, ratio * 200))

  const head =
    dir === 'flat'
      ? `与上年基本持平（${cell.change}）`
      : `比上年${dir === 'up' ? '增加' : '减少'} ${formatRatio(cell.change)}`

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

/**
 * 按报表分组。**走势图的下拉和下方的网格共用它**——
 * 各写一套的话两处的分组顺序会不一样（一边按科目名排、一边按解析顺序），
 * 而页面上看起来都对。
 */
function groupByStatement(
  grid: Pick<FactGrid, 'metrics'> | null,
): [string, FactGridRow[]][] {
  const buckets = new Map<string, FactGridRow[]>()
  for (const row of grid?.metrics ?? []) {
    const list = buckets.get(row.statement)
    if (list) list.push(row)
    else buckets.set(row.statement, [row])
  }
  for (const rows of buckets.values()) {
    rows.sort((a, b) => a.label_cn.localeCompare(b.label_cn, 'zh'))
  }
  // 按报表分组保持稳定顺序；组内按科目名排，和领域习惯一致
  const order = ['income', 'balance', 'cashflow', 'indicator', 'industry', 'disclosure']
  return [...buckets.entries()].sort(
    (a, b) => order.indexOf(a[0]) - order.indexOf(b[0]),
  )
}
