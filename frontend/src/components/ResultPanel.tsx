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
  asNarrative,
  asSeries,
  unwrap,
  type EvidenceSource,
  type MarginPoint,
  type NarrativeObservation,
  type SeriesPoint,
} from '../types/view'

/* ---- A 股配色：红涨绿跌 ----
 *
 * 与欧美市场相反，也与「红=危险」的通用界面语义相反。这里按 A 股来：
 * 用这套系统的人眼里，绿色柱子就是「跌」。
 *
 * ⚠ 颜色只是**同一个数字的第二种呈现**，不承载任何计算。
 *   `barColor` 做的是「后一年比前一年大还是小」这一下比较，
 *   没有算增长率、没有算差额——真正算数的地方在后端 `app/engine/`。
 *   首年没有上期，用中性灰，**不假装它是涨的**。
 */
const UP = '#e04a4a'
const DOWN = '#12a05c'
const FLAT = '#94a3b8'
const TREND = '#2f6fd0'

function barColor(curr: string | null, prev: string | null | undefined): string {
  if (curr == null || prev == null) return FLAT
  const a = Number(curr)
  const b = Number(prev)
  if (!Number.isFinite(a) || !Number.isFinite(b)) return FLAT
  return a > b ? UP : a < b ? DOWN : FLAT
}

/** 柱状图统一叠一条趋势线：柱子回答「这一年涨没涨」，线回答「这一路怎么走」。 */
function barSeries(data: { value: number | null; itemStyle: { color: string; borderRadius: number[] } }[]) {
  return { type: 'bar' as const, barMaxWidth: 34, data, z: 2 }
}

function trendSeries(values: (number | null)[]) {
  return {
    type: 'line' as const,
    name: '趋势',
    smooth: false,
    symbolSize: 6,
    data: values,
    z: 3,
    itemStyle: { color: TREND },
    lineStyle: { width: 2 },
  }
}

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
      barSeries(
        points.map((p, i) => ({
          value: p.value == null ? null : Number(p.value),
          itemStyle: {
            color: barColor(p.value, i > 0 ? points[i - 1].value : null),
            borderRadius: [3, 3, 0, 0],
          },
        })),
      ),
      trendSeries(points.map((p) => (p.value == null ? null : Number(p.value)))),
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
      barSeries(
        points.map((p, i) => ({
          value: p.margin == null ? null : Number(p.margin),
          itemStyle: {
            color: barColor(p.margin, i > 0 ? points[i - 1].margin : null),
            borderRadius: [3, 3, 0, 0],
          },
        })),
      ),
      trendSeries(points.map((p) => (p.margin == null ? null : Number(p.margin)))),
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

// ------------------------------------------------------------------ 叙事一致性

/* 五种状态的配色。**冲突用红色、支持用绿色，与上面的 K 线红绿相反**——
 * 那不是笔误：上面管的是「数字涨没涨」，这里管的是「管理层说对了没有」。
 * 两套语义混用会让人以为红色柱子代表叙事有问题。
 *
 * 顺序即严重程度，表格默认按它排：冲突排最前。评审的注意力有限，
 * 而这张表存在的意义就是让他先看到对不上的那几条。 */
const STATE_STYLE: Record<string, { cls: string; order: number }> = {
  conflicted: { cls: 'st-bad', order: 0 },
  partial: { cls: 'st-partial', order: 1 },
  incomparable: { cls: 'st-na', order: 2 },
  missing: { cls: 'st-na', order: 3 },
  supported: { cls: 'st-ok', order: 4 },
}

function obsTarget(o: NarrativeObservation): EvidenceTarget {
  // 一张对照表要能核对**两个**东西：管理层真这么说了，以及数字真是这样。
  // 只给其中一个的话，用户能看见结论却验不了它。
  const sources: EvidenceSource[] = [
    {
      role: `管理层原话 · ${o.source_period} 年报`,
      source_file: o.source_file,
      source_page: o.page_no,
      source_text: o.text,
    },
  ]
  const ms = o.metric_source
  if (ms?.derived && ms.sources?.length) {
    // 派生的毛利率出处是两行，与 facts.margin 一致——只留一个就是半个算式
    sources.push(...ms.sources)
  } else if (ms?.source_page) {
    sources.push({
      role: `验证依据 · ${o.metric_label}（${o.verify_period} 年）`,
      fact_id: ms.fact_id,
      source_file: ms.source_file,
      source_page: ms.source_page,
      source_table: ms.source_table,
      source_text: ms.source_text,
    })
  }
  return {
    title: `${o.source_period} 年 · ${o.theme_label}`,
    value: `${o.state_cn}｜${o.metric_label} ${o.actual || '—'}`,
    formula: o.formula,
    inputs: o.inputs,
    note: o.reason,
    sources,
  }
}

function NarrativeCard({
  n,
  onPick,
}: {
  n: NonNullable<ReturnType<typeof asNarrative>>
  onPick: (t: EvidenceTarget) => void
}) {
  const [showAll, setShowAll] = useState(false)
  const rows = [...n.observations].sort(
    (a, b) => (STATE_STYLE[a.state]?.order ?? 9) - (STATE_STYLE[b.state]?.order ?? 9),
  )
  const shown = showAll ? rows : rows.filter((o) => o.state === 'conflicted').slice(0, 6)
  const hidden = rows.length - shown.length

  const bad = n.counts.conflicted ?? 0
  const tone = bad === 0 ? 'ok' : 'bad'

  return (
    <section className="panel card narr">
      <header className="card-head">
        <h3>财报叙事一致性</h3>
        <p className="card-headline">
          共 {n.total} 条可验证主张
          <em className={tone === 'ok' ? 'up' : 'down'}> · {n.headline}</em>
        </p>
      </header>

      <div className={`verdict ${tone}`}>
        <b>{n.headline}</b>
        <span>
          {['supported', 'partial', 'conflicted', 'incomparable', 'missing'].map((s) => {
            const c = n.counts[s] ?? 0
            if (!c) return null
            return (
              <em key={s} className={STATE_STYLE[s]?.cls}>
                {s === 'supported' && '支持 '}
                {s === 'partial' && '部分支持 '}
                {s === 'conflicted' && '冲突 '}
                {s === 'incomparable' && '不可比 '}
                {s === 'missing' && '缺失 '}
                {c}
              </em>
            )
          })}
        </span>
      </div>

      {/* 按主题分组：评审要看的不是「33 条里 8 条冲突」，而是
          「哪一类说法系统性地对不上」——那才是有投资含义的东西。 */}
      <div className="themes">
        {n.themes.map((t) => (
          <div key={t.theme_key} className="theme">
            <div className="theme-head">
              <b>{t.label_cn}</b>
              <span className="muted small">
                共 {t.total} 条 · 验 {t.metric_label}
              </span>
            </div>
            <div className="theme-bar">
              {['conflicted', 'partial', 'incomparable', 'missing', 'supported'].map((s) => {
                const c = t.counts[s] ?? 0
                if (!c) return null
                return (
                  <i
                    key={s}
                    className={STATE_STYLE[s]?.cls}
                    style={{ flexGrow: c }}
                    title={`${s} ${c}`}
                  />
                )
              })}
            </div>
            <p className="muted small">{t.basis_cn}</p>
          </div>
        ))}
      </div>

      <table className="grid narr-grid">
        <thead>
          <tr>
            <th>判定</th>
            <th>年度</th>
            <th>管理层原话（点开看原文与出处）</th>
            <th>验证指标</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((o, i) => (
            <tr key={`${o.theme_key}-${o.source_period}-${o.page_no}-${i}`}
                className="clickable" onClick={() => onPick(obsTarget(o))}>
              <td>
                <span className={`st ${STATE_STYLE[o.state]?.cls}`}>{o.state_cn}</span>
              </td>
              <td>
                {o.source_period}
                {o.forward && <span className="tag tag-warn">前瞻</span>}
              </td>
              <td className="narr-text">
                {o.text}
                <span className="tag tag-match">命中「{o.matched}」</span>
              </td>
              <td className="narr-actual">
                <span className="muted small">{o.metric_label}</span>
                <br />
                {o.state === 'conflicted' || o.state === 'supported' ? o.actual : o.reason}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {hidden > 0 && (
        <button className="chip" onClick={() => setShowAll((v) => !v)}>
          {showAll ? '只看相悖的' : `展开其余 ${hidden} 条`}
        </button>
      )}

      {/* ⚠ 诊断指数为什么不出分，必须写在界面上。
          不写的话，用户看到「33 条主张、8 条冲突」会默认这就是全部结论，
          而完整的指数还含 R/P/Q 三项构成（共 30 分权重）没接上。 */}
      {n.index_note && <p className="hint warn-box">{n.index_note}</p>}
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
  const narrative = results.map((r) => asNarrative(r.value)).find(Boolean) ?? null

  // 最后一步是 Skill 自己合成的结论（不经过工具，因而没有 {result,...} 那一层）。
  //
  // ⚠ 有叙事卡时不显示它：叙事卡自己带了抬头与逐条判定，
  //   而 Skill 的摘要是同一批信息的文字版。两份并排出现，用户会去找
  //   「哪个才是结论」——一份界面给出两个说法，就已经输了一半。
  const last = results[results.length - 1]
  const conclusion =
    last && !narrative && !unwrap(last.value ?? {})?.points ? last.summary : null

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

      {narrative && <NarrativeCard n={narrative} onPick={onPick} />}

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
