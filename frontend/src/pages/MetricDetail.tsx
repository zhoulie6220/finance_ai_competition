import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'

import { useApi } from '../api/client'
import type { FactCell, FactGrid, FactGridRow } from '../api/types'
import AsyncBoundary from '../components/AsyncBoundary'
import { EChart } from '../components/EChart'
import EvidenceModal from '../components/EvidenceModal'
import { formatRatio, groupDigits } from '../format'
import { PRICE } from '../theme/colors'
import type { EChartsCoreOption } from 'echarts/core'

/**
 * 单个指标的细节：这些年的走势 + 逐年明细。
 *
 * 指标是**在左边选的**（侧栏那个下拉），这一页只负责显示。
 *
 * ## ⚠ 红绿在这一页的含义
 *
 * 这里是**涨跌色**：红 = 比上年增加，绿 = 减少。
 * 判定表里那个红绿是**语义色**（相悖 / 支持），两套体系，别混。
 * 见 `theme/colors.ts` 开头那张表。
 *
 * ## ⚠ 图上的每一个数都是后端给的
 *
 * 增速（`cell.change`）、方向（`cell.change_dir`）、算式（`change_formula`）、
 * 代入的数（`change_inputs`）都由 `backend/app/db/repository.py` 用
 * `engine.ratios.yoy_growth` 算好。**前端一个数都不算**——同比属于业务计算，
 * 在浏览器里算的东西没法审计，而「计算复算、过程可追溯」是比赛的硬要求。
 */
export default function MetricDetail() {
  const { projectId, metricKey } = useParams<{
    projectId: string
    metricKey: string
  }>()
  const [factId, setFactId] = useState<string | null>(null)

  const grid = useApi<FactGrid>(
    projectId ? `/projects/${projectId}/fact-grid` : null,
  )

  const row = useMemo(
    () => grid.data?.metrics.find((m) => m.metric_key === metricKey) ?? null,
    [grid.data, metricKey],
  )

  return (
    <div className="page">
      <AsyncBoundary loading={grid.loading} error={grid.error} onRetry={grid.reload}>
        {grid.data && !row && (
          <div className="async-state async-empty">
            <div className="async-detail">
              字段字典里没有 <code>{metricKey}</code> 这个指标。
              从左边重新选一个。
            </div>
          </div>
        )}

        {grid.data && row && (
          <>
            <header className="page-head">
              <h2>
                {row.label_cn}
                <span className="subtitle">
                  {grid.data.company_name}
                  {grid.data.periods[0]}–{grid.data.periods[grid.data.periods.length - 1]}
                  　{row.filled}/{grid.data.periods.length} 年有数据
                </span>
              </h2>
              <p className="page-note">
                柱子颜色是<strong>比上年</strong>：红增绿减。悬停或点表格里的数字
                可以看到算式、代入的数和年报出处。
              </p>
            </header>

            {row.filled === 0 && (
              <div className="async-state async-empty">
                <div className="async-detail">
                  <b>这一项还没采集。</b>
                  91 个指标里有近一半是空的——不是这家公司没披露，
                  是解析阶段还没覆盖到它。左边下拉里标着「未采集」的都是这一类。
                </div>
              </div>
            )}

            {row.filled > 0 && (
              <>
                <section className="card">
                  <h3>
                    走势
                    <span className="subtitle">{row.metric_key}</span>
                  </h3>
                  <div className="card-body">
                    <TrendChart row={row} periods={grid.data.periods} />
                    <p className="chart-note">
                      ⚠ 这里的红绿是<b>涨跌色</b>（数值比上年），
                      与「叙事一致性」页判定用的<b>语义色</b>（相悖 / 支持）
                      是两套体系。
                    </p>
                  </div>
                </section>

                <section className="card">
                  <h3>
                    逐年明细
                    <span className="subtitle">
                      点「年报出处」回到那一页原文
                    </span>
                  </h3>
                  <table className="plain-table">
                    <thead>
                      <tr>
                        <th>年度</th>
                        <th className="num">数值</th>
                        <th className="num">比上年</th>
                        <th>算式与代入的数</th>
                        <th>年报出处</th>
                      </tr>
                    </thead>
                    <tbody>
                      {grid.data.periods.map((p) => (
                        <Row
                          key={p}
                          period={p}
                          cell={row.cells[p]}
                          percent={row.unit_kind === 'percent'}
                          onOpen={setFactId}
                        />
                      ))}
                    </tbody>
                  </table>
                </section>
              </>
            )}
          </>
        )}
      </AsyncBoundary>

      <EvidenceModal factId={factId} onClose={() => setFactId(null)} />
    </div>
  )
}

// ---------------------------------------------------------------- 明细行

function Row({
  period,
  cell,
  percent,
  onOpen,
}: {
  period: string
  cell: FactCell | undefined
  percent: boolean
  onOpen: (id: string) => void
}) {
  const show = (v: string) => (percent ? formatRatio(v) : groupDigits(v))

  if (!cell || cell.status === 'not_found' || cell.value === null) {
    return (
      <tr>
        <td className="mono">{period}</td>
        <td className="num cell-notfound" title="未在年报中定位到，不是 0">
          —
        </td>
        <td colSpan={3} className="msg">
          未在年报中定位到，<b>不是 0</b>
        </td>
      </tr>
    )
  }

  const dir = cell.change_dir
  const color = dir === 'up' ? PRICE.up : dir === 'down' ? PRICE.down : PRICE.flat
  const word = dir === 'up' ? '增加' : dir === 'down' ? '减少' : '基本持平'

  return (
    <tr>
      <td className="mono">{period}</td>
      <td className="num">
        {cell.status === 'validated' && cell.fact_id ? (
          <button
            type="button"
            className="cell-button"
            onClick={() => onOpen(cell.fact_id!)}
          >
            {show(cell.value)}
          </button>
        ) : (
          <span className="cell-review" title={`状态：${cell.status}`}>
            {show(cell.value)}
          </span>
        )}
      </td>
      <td className="num mono" style={dir ? { color } : undefined}>
        {cell.change ? `${word} ${formatRatio(cell.change)}` : '—'}
      </td>
      <td className="msg">
        {cell.change ? (
          <>
            <span className="mono" style={{ fontSize: 'var(--fs-meta)' }}>
              {cell.change_formula}
            </span>
            <div className="tight" style={{ color: 'var(--ink-3)' }}>
              {Object.entries(cell.change_inputs)
                .map(([k, v]) => `${k} = ${v}`)
                .join('　')}
            </div>
          </>
        ) : (
          <span className="tight" style={{ color: 'var(--ink-3)' }}>
            {cell.change_refused ?? '—'}
          </span>
        )}
      </td>
      <td className="tight">
        {cell.source_page ? `第 ${cell.source_page} 页` : '—'}
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------- 走势图

function TrendChart({ row, periods }: { row: FactGridRow; periods: string[] }) {
  const option = useMemo<EChartsCoreOption>(() => {
    const percent = row.unit_kind === 'percent'

    // ⚠ 只做**格式化**：`Number()` 是把后端给的字符串转成图能用的数，
    //   `formatRatio` 是把 0.0739 显示成 7.39%——两者都不产生新信息。
    //   真正算出来的东西（增速、方向、算式）全部来自后端。
    const show = (v: number) =>
      percent ? formatRatio(String(v)) : groupDigits(String(v))

    const bars = periods.map((p) => {
      const c = row.cells[p]
      const dir = c?.change_dir ?? null
      return {
        value: c?.value != null ? Number(c.value) : null,
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
          const head = `<b>${row.label_cn}</b>　${p}<br/>数值：${show(Number(c.value))}`
          const src = c.source_file
            ? `<br/><span style="color:#767676">出处：${c.source_file} 第 ${c.source_page} 页</span>`
            : ''
          if (!c.change) {
            return (
              head +
              (c.change_refused
                ? `<br/><span style="color:#d46b08">不出同比：${c.change_refused}</span>`
                : '') +
              src
            )
          }
          const dir =
            c.change_dir === 'up' ? '增加' : c.change_dir === 'down' ? '减少' : '基本持平'
          const color =
            c.change_dir === 'up' ? PRICE.up
              : c.change_dir === 'down' ? PRICE.down : PRICE.flat
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
          formatter: (v: number) =>
            percent ? formatRatio(String(v)) : groupDigits(String(v)),
        },
      },
      series: [{ type: 'bar', barMaxWidth: 44, data: bars, cursor: 'default' }],
    }
  }, [row, periods])

  return <EChart option={option} height={260} />
}
