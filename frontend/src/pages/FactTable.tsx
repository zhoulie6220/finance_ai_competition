import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'

import { useApi } from '../api/client'
import type { FactCell, FactGrid, FactGridRow } from '../api/types'
import AsyncBoundary from '../components/AsyncBoundary'
import { EChart } from '../components/EChart'
import EvidenceModal from '../components/EvidenceModal'
import { groupDigits, formatRatio, statementLabel } from '../format'
import { PRICE } from '../theme/colors'
import type { EChartsCoreOption } from 'echarts/core'

/**
 * 财务事实：上面是一句话能看懂的走势，下面是能点回原文的网格。
 *
 * 两块的**数据是同一份**（`/fact-grid`），只是用途不同：
 *   走势图 —— 选一个指标，看它这些年的变化，柱子红增绿减（同花顺那样）
 *   网格   —— 91 个指标 × 10 个年度，点任意一格回到年报原文
 *
 * ## ⚠ 红绿在这一页的含义
 *
 * 这里是**涨跌色**：红 = 比上年增加，绿 = 减少。
 * 判定表里那个红绿是**语义色**（相悖 / 支持），两套体系，别混。
 * 见 `theme/colors.ts` 开头那张表。
 *
 * ## ⚠ 柱子上的数字全是后端给的
 *
 * 增速（`cell.change`）、方向（`cell.change_dir`）、算式（`change_formula`）、
 * 代入的数（`change_inputs`）都由 `backend/app/db/repository.py` 用
 * `engine.ratios.yoy_growth` 算好。**前端一个数都不算**——同比属于业务计算，
 * 在浏览器里算的东西没法审计，而「计算可复算、过程可追溯」是比赛的硬要求。
 *
 * 四种格子状态必须视觉可分且绝不混淆：有值 / 未找到（**不是 0**）/ 待复核 / 不可比。
 * 「不是 0」那句话不是装饰：填成 0 看起来最省事，但毛利率会因此变成
 * 负百分之百，而没有任何地方会提示。
 */
export default function FactTable() {
  const { projectId } = useParams<{ projectId: string }>()
  const [factId, setFactId] = useState<string | null>(null)
  const [year, setYear] = useState<string | null>(null)
  const [metricKey, setMetricKey] = useState<string | null>(null)

  const grid = useApi<FactGrid>(
    projectId ? `/projects/${projectId}/fact-grid` : null,
  )

  const grouped = useMemo(() => groupByStatement(grid.data), [grid.data])

  // 默认选中「营业总收入」——演示从它开始最自然。
  // 找不到就退回第一行，不写死键名：换一家公司时字段字典可能不同，
  // 写死会选中一个不存在的指标，图上是空的，而且不报错。
  useEffect(() => {
    if (!grid.data || metricKey) return
    const preferred = grid.data.metrics.find((m) => m.metric_key === 'revenue')
    setMetricKey((preferred ?? grid.data.metrics[0])?.metric_key ?? null)
  }, [grid.data, metricKey])

  const row = useMemo(
    () => grid.data?.metrics.find((m) => m.metric_key === metricKey) ?? null,
    [grid.data, metricKey],
  )

  return (
    <div className="page">
      <header className="page-head">
        <h2>
          财务事实
          {grid.data && (
            <span className="subtitle">
              {grid.data.company_name}（{grid.data.periods[0]}–
              {grid.data.periods[grid.data.periods.length - 1]}）
            </span>
          )}
        </h2>
        <p className="page-note">
          选一个指标看它这些年的走势；点网格里任意一个有值的格子，回到年报原文。
          灰色「—」表示未在年报中定位到，<strong>不是 0</strong>。
        </p>
      </header>

      <AsyncBoundary loading={grid.loading} error={grid.error} onRetry={grid.reload}>
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
          <>
            {/* ---- 走势 ---- */}
            <section className="card">
              <h3>
                数据走势
                <span className="subtitle">
                  柱子颜色是<b>比上年</b>：红增绿减。悬停看算式与出处
                </span>
              </h3>
              <div className="card-body">
                <MetricPicker
                  rows={grid.data.metrics}
                  total={grid.data.periods.length}
                  value={metricKey}
                  onChange={setMetricKey}
                />
                {row && (
                  <TrendChart row={row} periods={grid.data.periods} />
                )}
                <p className="chart-note">
                  ⚠ 这里的红绿是<b>涨跌色</b>（数值比上年），
                  与「叙事一致性」页判定用的<b>语义色</b>（相悖 / 支持）是两套体系。
                </p>
              </div>
            </section>

            {/* ---- 网格 ---- */}
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
                              cell={r.cells[period]}
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
            </section>
          </>
        )}
      </AsyncBoundary>

      <EvidenceModal factId={factId} onClose={() => setFactId(null)} />
    </div>
  )
}

// ---------------------------------------------------------------- 指标下拉

/** 指标下拉。91 个指标按报表分组，和网格里的分组一致。 */
function MetricPicker({
  rows,
  total,
  value,
  onChange,
}: {
  rows: FactGridRow[]
  /** 一共几个年度。用来显示「8/10」这个覆盖数 */
  total: number
  value: string | null
  onChange: (key: string) => void
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  const current = rows.find((r) => r.metric_key === value) ?? null

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // 与下方网格共用同一个分组函数——两处各写一套的话，
  // 分组顺序会不一样，而页面上看起来都对。
  const groups = useMemo(() => groupByStatement({ metrics: rows }), [rows])

  return (
    <div className="metric-picker" ref={box}>
      <button
        type="button"
        className="metric-select"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="nm">{current?.label_cn ?? '选择指标'}</span>
        <span className="key">{current?.metric_key ?? ''}</span>
        <span className="caret">⌄</span>
      </button>

      {open && (
        <ul className="metric-menu">
          {groups.map(([statement, list]) => (
            <li key={statement}>
              <div className="grp">{statementLabel(statement)}</div>
              {list.map((m) => (
                <button
                  key={m.metric_key}
                  type="button"
                  className={
                    (m.metric_key === value ? 'on' : '') +
                    (m.filled === 0 ? ' empty' : '')
                  }
                  onClick={() => {
                    onChange(m.metric_key)
                    setOpen(false)
                  }}
                >
                  {m.label_cn}
                  {/* 覆盖数：**后端数好的**，前端不重算。
                      没有它的话，选到一个空指标会看到一张空图，
                      而那看起来像系统坏了 */}
                  <span className="fill mono">
                    {m.filled === 0 ? '未采集' : `${m.filled}/${total}`}
                  </span>
                  <span className="key">{m.metric_key}</span>
                </button>
              ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 走势图

/**
 * 一个指标的逐年柱状图，柱子按**后端给的方向**上色。
 *
 * 用的是项目里已有的 `EChart` 薄封装（echarts 早就在依赖里，
 * 之前只被一段死代码引用）。
 */
function TrendChart({ row, periods }: { row: FactGridRow; periods: string[] }) {
  const option = useMemo<EChartsCoreOption>(() => {
    const percent = row.unit_kind === 'percent'

    // ⚠ 只做**格式化**：`Number()` 是把后端给的字符串转成图能用的数，
    //   `formatRatio` 是把 0.0739 显示成 7.39%——两者都不产生新信息。
    //   真正算出来的东西（增速、方向、算式）全部来自后端。
    const show = (v: number) => (percent ? formatRatio(String(v)) : groupDigits(String(v)))

    const values = periods.map((p) => {
      const c = row.cells[p]
      return c?.value != null ? Number(c.value) : null
    })

    const bars = periods.map((p, i) => {
      const c = row.cells[p]
      const dir = c?.change_dir ?? null
      return {
        value: values[i],
        itemStyle: {
          color:
            dir === 'up' ? PRICE.up
              : dir === 'down' ? PRICE.down
                : dir === 'flat' ? PRICE.flat
                  : '#d9d9d9',   // 没有同比（第一个年度 / 算不出来）
        },
      }
    })

    return {
      grid: { left: 8, right: 8, top: 16, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        // 悬停里必须有**算式与代入的数**——「点结论回到计算过程」
        // 在这一页的落点就是它。只给一个百分比的话，看的人没法核对。
        formatter: (ps: unknown) => {
          const arr = ps as { dataIndex: number }[]
          const i = arr?.[0]?.dataIndex ?? 0
          const p = periods[i]
          const c = row.cells[p]
          if (!c || c.value == null) {
            return `<b>${p}</b><br/>未在年报中定位到（不是 0）`
          }
          const head = `<b>${row.label_cn}</b>　${p}<br/>金额/数量：${show(Number(c.value))}`
          const src = c.source_file
            ? `<br/><span style="color:#767676">出处：${c.source_file} 第 ${c.source_page} 页</span>`
            : ''
          if (!c.change) {
            const why = c.change_refused
              ? `<br/><span style="color:#d46b08">不出同比：${c.change_refused}</span>`
              : ''
            return head + why + src
          }
          const dir = c.change_dir === 'up' ? '增加' : c.change_dir === 'down' ? '减少' : '基本持平'
          const color = c.change_dir === 'up' ? PRICE.up : c.change_dir === 'down' ? PRICE.down : PRICE.flat
          const inputs = Object.entries(c.change_inputs)
            .map(([k, v]) => `${k} = ${v}`)
            .join('<br/>')
          return (
            `${head}<br/>比上年<b style="color:${color}">${dir} ${formatRatio(c.change)}</b>` +
            `<br/><span style="color:#767676">${c.change_formula ?? ''}</span>` +
            (inputs ? `<br/><span style="color:#767676">${inputs}</span>` : '') +
            src
          )
        },
      },
      xAxis: {
        type: 'category',
        data: periods,
        axisTick: { show: false },
        axisLine: { lineStyle: { color: '#e0e0e0' } },
        axisLabel: { color: '#595959', fontSize: 11 },
      },
      yAxis: {
        type: 'value',
        scale: true,   // 不从 0 起：年报数字都在同一个量级上，从 0 起会把差异压平
        splitLine: { lineStyle: { color: '#f0f0f0' } },
        axisLabel: {
          color: '#595959',
          fontSize: 11,
          formatter: (v: number) => (percent ? formatRatio(String(v)) : groupDigits(String(v))),
        },
      },
      series: [
        {
          type: 'bar',
          barMaxWidth: 44,
          data: bars,
          cursor: 'default',
        },
      ],
    }
  }, [row, periods])

  return <EChart option={option} height={220} />
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
