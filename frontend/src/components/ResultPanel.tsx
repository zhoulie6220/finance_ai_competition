/**
 * 结果面板：把每一步的结构化产出渲染成能核对的界面。
 *
 * 之前的版本把 `step.output`（一句中文摘要）原样打在 `<pre>` 里，于是页面上
 * 只有「营业收入 2014–2024：187,414 → 322,116 百万元」这样一行字——
 * 看着挺像回事，但**没有任何办法核对它**。而本系统的卖点恰恰是「点任意结论
 * 回到年报原文」：摘要不给公式、不给入参、不给页码，等于把卖点丢在传输路上。
 *
 * 所以这里渲染的是 `step.succeeded` 事件里那个 `value`，它是工具真正的产出。
 *
 * ## 前端仍然不做任何计算
 *
 * 同比、毛利率、覆盖统计全部由后端 `app/engine/` 与 Skill 算好。
 * 这里只做两件纯展示的事：
 *
 *   1. **千分位与小数位**（`Intl.NumberFormat`）——不改变数值本身
 *   2. **把数字摆到图上的坐标里**——图表本来就是视觉映射，不是计算
 *
 * 唯一一处「加工」是判断某个字段在不在（`in points[0]`）来决定渲染哪种卡片，
 * 那是分派，不是运算。
 */

import { useState } from 'react'
import { EChart } from './EChart'
import type { EvidenceTarget } from './EvidenceDrawer'
import type { StepResult } from '../hooks/useTaskStream'
import {
  asCoverage,
  asMargin,
  asSeries,
  unwrap,
  type EvidenceSource,
  type MarginPoint,
  type SeriesPoint,
} from '../types/view'

/** 金额一律按后端给的单位显示，这里只加千分位与两位小数。 */
const NUM = new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
})

function fmt(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? NUM.format(n) : String(value)
}

/** 百分数：后端给的是 "5.4544"（表示 5.4544%），只补一个 % 号。 */
function pct(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const n = Number(value)
  return Number.isFinite(n) ? `${NUM.format(n)}%` : String(value)
}

/** 同比：后端给的是小数（-0.065），这里乘 100 只为显示。 */
function signedPct(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const n = Number(value) * 100
  if (!Number.isFinite(n)) return String(value)
  return `${n > 0 ? '+' : ''}${NUM.format(n)}%`
}

function sourcesOf(p: SeriesPoint | MarginPoint): EvidenceSource[] {
  if ('sources' in p && Array.isArray(p.sources) && p.sources.length) return p.sources
  return [p as EvidenceSource]
}

// ------------------------------------------------------------------ 子组件

function CoverageCard({ c }: { c: ReturnType<typeof asCoverage> }) {
  if (!c) return null
  return (
    <div className="cover">
      <div className="cover-main">
        <h2>{c.project_name}</h2>
        <p className="cover-sub">
          {c.company_name}
          {c.stock_code && <> · {c.stock_code}</>}
        </p>
      </div>
      <div className="cover-stats">
        <div>
          <b>{fmt(c.facts)}</b>
          <span>条已验证事实</span>
        </div>
        <div>
          <b>{c.metrics}</b>
          <span>个指标</span>
        </div>
        <div>
          <b>{c.years.length}</b>
          <span>个年度</span>
        </div>
        {c.years.length > 0 && (
          <div>
            <b>
              {c.years[0]}–{c.years[c.years.length - 1]}
            </b>
            <span>覆盖区间</span>
          </div>
        )}
      </div>
    </div>
  )
}

function SeriesCard({
  s,
  onPick,
}: {
  s: NonNullable<ReturnType<typeof asSeries>>
  onPick: (t: EvidenceTarget) => void
}) {
  const points = s.points
  const usable = points.filter((p) => p.value !== null && p.value !== undefined)
  const latest = usable[usable.length - 1]

  const option = {
    grid: { left: 68, right: 20, top: 24, bottom: 32 },
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${fmt(v)} ${s.unit}` },
    xAxis: {
      type: 'category',
      data: points.map((p) => p.period),
      axisLabel: { fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      name: s.unit,
      nameTextStyle: { fontSize: 11, color: '#8a94a6' },
      axisLabel: { fontSize: 11, formatter: (v: number) => NUM.format(v) },
      splitLine: { lineStyle: { color: '#eef1f6' } },
    },
    series: [
      {
        type: 'line',
        smooth: false,
        symbolSize: 7,
        data: points.map((p) => (p.value == null ? null : Number(p.value))),
        itemStyle: { color: '#2f6fd0' },
        lineStyle: { width: 2 },
        areaStyle: { opacity: 0.06 },
      },
    ],
  }

  return (
    <section className="panel card">
      <header className="card-head">
        <h3>{s.label_cn}</h3>
        {latest && (
          <p className="card-headline">
            {latest.period} 年 <b>{fmt(latest.value)}</b> {s.unit}
            {latest.yoy_note && <em className="muted">（同比不可比）</em>}
            {!latest.yoy_note && latest.yoy != null && (
              <em className={Number(latest.yoy) < 0 ? 'down' : 'up'}>
                {signedPct(latest.yoy)}
              </em>
            )}
          </p>
        )}
      </header>

      <EChart option={option} />

      <table className="grid">
        <thead>
          <tr>
            <th>年度</th>
            <th className="num">数值（{s.unit}）</th>
            <th className="num">同比</th>
            <th>出处</th>
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr
              key={p.period}
              className="clickable"
              onClick={() =>
                onPick({
                  title: `${p.period} 年 · ${s.label_cn}`,
                  value: `${fmt(p.value)} ${s.unit}`,
                  formula: p.yoy_formula,
                  note: p.restated ? p.restatement_note : p.yoy_note,
                  sources: sourcesOf(p),
                })
              }
            >
              <td>{p.period}</td>
              <td className="num">{fmt(p.value)}</td>
              <td className="num">
                {p.yoy_note ? (
                  // 不可比就**不显示数字**，显示理由本身。带星号的数字会被人直接拿去用。
                  //
                  // ⚠ 别在这里统一写「不可比」四个字：后端给了**具体**理由，
                  //   而它们说的不是一回事——「首年，无上期」是压根没有上期，
                  //   「基期为负，应看扭亏」是有上期但不能这么比，
                  //   「期间不连续」是隔了年。压成同一个词就把信息丢光了，
                  //   而这三种情况用户要做的事完全不同。
                  <span className="refused" title={p.yoy_note}>
                    {p.yoy_note}
                  </span>
                ) : (
                  signedPct(p.yoy)
                )}
              </td>
              <td className="src-cell">
                {p.source_page ? `第 ${p.source_page} 页` : '—'}
                {p.restated && <span className="tag tag-warn">重述</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function MarginCard({
  m,
  onPick,
}: {
  m: NonNullable<ReturnType<typeof asMargin>>
  onPick: (t: EvidenceTarget) => void
}) {
  const points = m.points
  const option = {
    grid: { left: 56, right: 20, top: 24, bottom: 32 },
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${fmt(v)}%` },
    xAxis: { type: 'category', data: points.map((p) => p.period), axisLabel: { fontSize: 11 } },
    yAxis: {
      type: 'value',
      name: '%',
      nameTextStyle: { fontSize: 11, color: '#8a94a6' },
      axisLabel: { fontSize: 11 },
      splitLine: { lineStyle: { color: '#eef1f6' } },
    },
    series: [
      {
        type: 'bar',
        barMaxWidth: 34,
        data: points.map((p) => (p.margin == null ? null : Number(p.margin))),
        itemStyle: { color: '#4a90d9', borderRadius: [3, 3, 0, 0] },
      },
    ],
  }

  return (
    <section className="panel card">
      <header className="card-head">
        <h3>{m.label_cn}</h3>
        <p className="card-headline muted">
          共 {points.filter((p) => p.margin != null).length} 个年度可比
          {points.some((p) => p.margin == null) &&
            `，${points.filter((p) => p.margin == null).length} 个年度拒绝出数`}
        </p>
      </header>

      {/* 「年报里没有毛利这一行」是这个指标的**固有性质**，不是某一年的情况，
          所以写在卡片上说明一次。逐行重复同一句话只会把表格挤满，
          而真正区分各行的信息（哪一年不可比、为什么）反而被淹掉。 */}
      {points[0]?.gross_is_derived && (
        <p className="muted small">
          年报没有「毛利」这一行：分子由<b>营业收入 − 营业成本</b>算得，
          两个来源都在证据面板里。点任意一行可查看。
        </p>
      )}

      <EChart option={option} />

      <table className="grid">
        <thead>
          <tr>
            <th>年度</th>
            <th className="num">毛利率</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr
              key={p.period}
              className="clickable"
              onClick={() =>
                onPick({
                  title: `${p.period} 年 · ${m.label_cn}`,
                  value: pct(p.margin),
                  formula: p.formula,
                  inputs: p.inputs,
                  note: p.note,
                  derived: p.gross_is_derived,
                  sources: sourcesOf(p),
                })
              }
            >
              <td>{p.period}</td>
              <td className="num">
                {p.margin == null ? <span className="refused">拒绝出数</span> : pct(p.margin)}
              </td>
              {/* 只有真有事要说时才写字。「—」比一句每行都一样的话信息量更大 */}
              <td className="src-cell">{p.note ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

// ------------------------------------------------------------------ 主组件

interface Props {
  results: StepResult[]
  onPick: (t: EvidenceTarget) => void
}

export function ResultPanel({ results, onPick }: Props) {
  const [showRaw, setShowRaw] = useState(false)

  if (!results.length) {
    return <p className="empty">任务执行时，每一步的产出会显示在这里。</p>
  }

  const coverage = results.map((r) => asCoverage(r.value)).find(Boolean) ?? null
  const series = results.map((r) => asSeries(r.value)).filter(Boolean) as NonNullable<
    ReturnType<typeof asSeries>
  >[]
  const margin = results.map((r) => asMargin(r.value)).find(Boolean) ?? null

  // 最后一步是 Skill 自己合成的结论（不经过工具，因而没有 {result,...} 那一层）
  const last = results[results.length - 1]
  const conclusion = last && !unwrap(last.value ?? {})?.points ? last.summary : null

  return (
    <>
      {conclusion && (
        <section className="panel card">
          <header className="card-head">
            <h3>结论</h3>
          </header>
          <pre className="conclusion">{conclusion}</pre>
        </section>
      )}

      <CoverageCard c={coverage} />

      {series.map((s) => (
        <SeriesCard key={s.metric_key} s={s} onPick={onPick} />
      ))}

      {margin && <MarginCard m={margin} onPick={onPick} />}

      <section className="panel card">
        <header className="card-head">
          <h3>每一步的原始产出</h3>
          <button className="chip" onClick={() => setShowRaw((v) => !v)}>
            {showRaw ? '收起' : '展开'}
          </button>
        </header>
        <p className="muted small">
          这里显示的是后端返回的结构化结果原样。界面上的图、表、证据面板全部由它渲染，
          <b>没有任何数字是前端算出来的</b>。
        </p>
        {showRaw &&
          results.map((r) => (
            <details key={r.seq} className="raw-step">
              <summary>
                <span className="step-seq">{r.seq}</span>
                {r.name}
              </summary>
              <pre className="step-out">{JSON.stringify(r.value, null, 2)}</pre>
            </details>
          ))}
      </section>
    </>
  )
}
