import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'

import { useApi } from '../api/client'
import type {
  ChecksResponse,
  ClaimMatch,
  ClaimsResponse,
  ComponentDetail,
  ExtractorSummary,
  MatchesResponse,
  NarrativeIndex,
  Verdict,
} from '../api/types'
import AsyncBoundary from '../components/AsyncBoundary'
import ErrorBoundary from '../components/ErrorBoundary'
import EvidenceModal from '../components/EvidenceModal'
import { CHECK, SEVERITY, VERDICT } from '../theme/colors'
import { groupDigits } from '../format'

/**
 * 叙事一致性诊断。
 *
 * 三块内容，从上到下就是演示的动线：
 *   ① 指数卡片 —— 要么出分，要么**如实说明为什么不出分**
 *   ② 分项表   —— H/C/R/P/Q 的取值与来源
 *   ③ 主张—事实对照表 —— 这个项目最有说服力的一屏
 *
 * ⚠ **判定用语义色，不用涨跌色。** 支持=绿、相悖=红、无明显变化=灰、
 * 待核查=橙、不可验证=蓝灰；而红涨绿跌描述的是**数值方向**。
 * 混用会让「相悖」那个红色被读成「下跌」——一个评委一眼能看出的理解错误。
 */

export default function Narrative() {
  const { projectId } = useParams<{ projectId: string }>()
  const [factId, setFactId] = useState<string | null>(null)
  const [filter, setFilter] = useState<Verdict | 'all'>('all')

  const index = useApi<NarrativeIndex>(
    projectId ? `/projects/${projectId}/narrative/index` : null,
  )
  const matches = useApi<MatchesResponse>(
    projectId ? `/projects/${projectId}/narrative/matches` : null,
  )
  // ⚠ **必须显式传 limit。** 后端默认 300，宝钢有 353 条 →
  //   53 条被**静默截掉**：那一节「未纳入判定的主张」少显示 53 条，
  //   而页面上看不出少了什么（条数没标）。分成两页显示的取数口只有这一个。
  const claims = useApi<ClaimsResponse>(
    projectId ? `/projects/${projectId}/narrative/claims` : null,
    useMemo(() => ({ limit: 2000 }), []),
  )
  const checks = useApi<ChecksResponse>(
    projectId ? `/checks` : null,
    useMemo(() => ({ project_id: projectId }), [projectId]),
  )

  const visible = useMemo(() => {
    const rows = matches.data?.matches ?? []
    return filter === 'all' ? rows : rows.filter((r) => r.verdict === filter)
  }, [matches.data, filter])

  return (
    <ErrorBoundary label="叙事一致性诊断">
    <div className="page">
      <header className="page-head">
        <h2>叙事一致性诊断</h2>
        <p className="page-note">
          系统把管理层在年报里的表述拆成可验证主张，与后续财务事实交叉验证。
          每一条都能点回到年报原文。
        </p>
      </header>

      <AsyncBoundary
        loading={index.loading}
        error={index.error}
        onRetry={index.reload}
      >
        {index.data && projectId && (
          <IndexCard data={index.data} projectId={projectId} />
        )}
      </AsyncBoundary>

      <VerdictBars counts={matches.data?.counts ?? null} />

      <section className="panel">
        <h3>
          主张—事实对照表
          {matches.data && (
            <span className="subtitle">
              共 {matches.data.matches.length} 条判定
            </span>
          )}
        </h3>

        <AsyncBoundary
          loading={matches.loading}
          error={matches.error}
          empty={!!matches.data && matches.data.matches.length === 0}
          emptyText={matches.data?.hint ?? '这次分析还没有生成判定结果。'}
          onRetry={matches.reload}
        >
          {matches.data && matches.data.matches.length > 0 && (
            <>
              <VerdictFilter
                counts={matches.data.counts}
                value={filter}
                onChange={setFilter}
              />
              <MatchTable rows={visible} onOpenFact={setFactId} />
            </>
          )}
        </AsyncBoundary>
      </section>

      <ThemeBars stats={claims.data?.stats ?? null} />

      <section className="panel">
        <h3>
          抽取法
          <span className="subtitle">确定性规则抽取，每个数字都能从年报重算</span>
        </h3>
        <AsyncBoundary
          loading={claims.loading}
          error={claims.error}
          onRetry={claims.reload}
        >
          {claims.data && <ExtractorComparison rows={claims.data.by_extractor} />}
        </AsyncBoundary>
      </section>

      <section className="panel">
        <h3>
          未纳入判定的主张
          <span className="subtitle">
            主判据的字段字典里没有对应指标，按 v1.1 不降级用代理指标硬判
          </span>
        </h3>
        <AsyncBoundary
          loading={claims.loading}
          error={claims.error}
          onRetry={claims.reload}
        >
          {claims.data && <BackgroundClaims data={claims.data} />}
        </AsyncBoundary>
      </section>

      <section className="panel">
        <h3>
          财务质量项（Q）的输入 —— 三表勾稽校验
          <span className="subtitle">诊断指数的 Q 项由此项产生</span>
        </h3>
        <AsyncBoundary
          loading={checks.loading}
          error={checks.error}
          onRetry={checks.reload}
        >
          {checks.data && <ChecksView data={checks.data} />}
        </AsyncBoundary>
      </section>

      <EvidenceModal factId={factId} onClose={() => setFactId(null)} />
    </div>
    </ErrorBoundary>
  )
}

// ---------------------------------------------------------------- 指数卡片

function IndexCard({ data, projectId }: { data: NarrativeIndex; projectId: string }) {
  const scored = data.status === 'scored'

  return (
    <section className="panel index-card">
      <div className={`index-score ${scored ? 'index-scored' : 'index-insufficient'}`}>
        {/* 闸门不过时**绝不显示 0 或 50**——页面上出现一个大号数字，
            人工复核的入口就没人看了 */}
        <div className="score-value">{scored ? data.score : '不出分'}</div>
        <div className="score-grade">
          {scored ? GRADE_LABELS[data.grade] ?? data.grade : '证据不足'}
        </div>
        {data.coverage && (
          <div className="score-coverage">覆盖率 {data.coverage}</div>
        )}
      </div>

      <div className="index-reason">
        {scored ? (
          <p className="index-formula">{data.formula}</p>
        ) : (
          <>
            <h3>为什么不出分</h3>
            {/* 闸门四条**逐条画勾叉**。这一块是刻意做的：
                只给一句「证据不足」加一段原因，读的人分不出
                「四项过了三项、只差一张表」和「差得远」——
                而这两件事该做的事完全不同。
                ⚠ 判定与阈值都在后端（engine/index.py），这里只渲染 passed。 */}
            {data.gate.length > 0 && (
              <div className="gate">
                {data.gate.map((g) => (
                  <div key={g.key} className={g.passed ? 'gate-row ok' : 'gate-row no'}>
                    <span className="mark">{g.passed ? '✓' : '✗'}</span>
                    <span className="lbl">{g.label_cn}</span>
                    <span className="det">{g.detail}</span>
                  </div>
                ))}
              </div>
            )}
            {/* Q2 的比较进度。**会计 2026-10-06 答复明确要求页面展示这个**
                （原话：「8/9组比较已完成，1组因原始披露缺失待核查」）。
                一句话后面挂着逐对的明细——只给「Q 不可算」的话，
                读的人分不出「差一对比」和「一对都没有数据」，
                而这两件事要做的事完全不同。
                ⚠ 它**不改变闸门结论**：Q 仍是不完整、指数仍不出分。 */}
            {data.q2_progress && (
              <details className="q2-progress" open={data.q2_progress.done < data.q2_progress.total}>
                <summary>{data.q2_progress.line}</summary>
                <ul>
                  {data.q2_progress.pairs.map((p) => (
                    <li key={`${p.prior}-${p.current}`} className={p.done ? 'ok' : 'no'}>
                      <span className="mark">{p.done ? '✓' : '✗'}</span>
                      <span className="who mono">{p.prior}→{p.current}</span>
                      <span className="why">{p.done ? '已判定' : p.reason}</span>
                    </li>
                  ))}
                </ul>
              </details>
            )}

            {/* 原因**逐条**列出，不是笼统一句「证据不足」——
                只说要补证据，没人知道补什么 */}
            <ul className="reason-list">
              {(data.insufficient_reason ?? '').split('；').filter(Boolean).map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </>
        )}

        <div className="index-counts">
          <Count label="有效观测 n" value={data.counts.n} />
          <Count label="覆盖率分母 N" value={data.counts.N} />
          <Count label="H 观测" value={data.counts.history_observations} />
          <Count label="C 观测" value={data.counts.current_observations} />
          {data.counts.skipped_no_period > 0 && (
            <Count label="无期间表述" value={data.counts.skipped_no_period} muted />
          )}
          {data.counts.skipped_no_fact > 0 && (
            <Count label="主判据无数据" value={data.counts.skipped_no_fact} muted />
          )}
        </div>

        <ComponentTable data={data} projectId={projectId} />

        {/* 估值动作必须显示——指数的作用就是**透明地**影响估值，
            隐去这一步等于把它变回黑箱 */}
        <div className="index-action">
          <div>
            <span className="label">工作台动作</span>
            {data.action}
          </div>
          <div>
            <span className="label">估值动作</span>
            {data.valuation_action}
          </div>
          {data.scenarios.weights ? (
            <div>
              <span className="label">情景权重</span>
              基准 {data.scenarios.weights.base} / 乐观{' '}
              {data.scenarios.weights.optimistic} / 压力 {data.scenarios.weights.stress}
              {data.scenarios.revenue_growth_ref && (
                <span className="hint">
                  　增长假设参照 {data.scenarios.revenue_growth_ref}
                </span>
              )}
            </div>
          ) : (
            <div>
              <span className="label">情景权重</span>
              <span className="muted">
                不提供 —— 不足以出分时不做任何由指数驱动的估值调整
              </span>
            </div>
          )}
          <div className="hint">{data.user_hint}</div>
          {data.scenarios.requires_human_confirmation && (
            <div className="requires-confirm">
              需人工确认后才重算，**不会自动写入估值**
            </div>
          )}
        </div>

        {/* 约束说明随结果一起给出，不做折叠——它们划定了这条结论的边界 */}
        <details className="scenario-notes" open>
          <summary>传导的边界（这些参数**没有**被指数改动）</summary>
          <ul>
            {data.scenarios.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </details>

        <p className="conclusion-boundary">{data.conclusion_boundary}</p>
      </div>
    </section>
  )
}

const GRADE_LABELS: Record<string, string> = {
  high: '已核验样本的一致性支持较强',
  medium: '支持与风险并存，需关注',
  low: '已核验样本支持较弱，优先复核',
  insufficient_evidence: '证据不足',
}

function Count({
  label,
  value,
  muted = false,
}: {
  label: string
  value: number
  muted?: boolean
}) {
  return (
    <div className={muted ? 'count count-muted' : 'count'}>
      <span className="count-value">{value}</span>
      <span className="count-label">{label}</span>
    </div>
  )
}

/** H/C 取值 [−1,1]，R/P/Q 取值 [0,1]。**两组的范围不同，展示时要说清楚。** */
const COMPONENT_ROWS: Array<{
  key: keyof NarrativeIndex['components']
  /** 后端下钻端点的路径段。**与 `matching.COMPONENT_META` 的键一一对应。** */
  drill: string
  label: string
  range: string
  meaning: string
}> = [
  { key: 'history', drill: 'h', label: 'H 历史兑现度', range: '[−1, 1]', meaning: '以前报告的前瞻主张，用后来的实际结果验证' },
  { key: 'current', drill: 'c', label: 'C 当期一致性', range: '[−1, 1]', meaning: '本期主张与同期财务事实的匹配程度' },
  { key: 'risk', drill: 'r', label: 'R 风险披露充分度', range: '[0, 1]', meaning: '四项风险检查中说明了对象、路径与依据的占比' },
  { key: 'template', drill: 'p', label: 'P 缺乏可验证性', range: '[0, 1]', meaning: '缺乏可验证对象、期间或结果的实质表述占比' },
  { key: 'quality', drill: 'q', label: 'Q 财务质量冲突', range: '[0, 1]', meaning: '已确认触发的质量检查占比' },
]

/**
 * 分项表。**整行可点**——点开是这一项的构成明细。
 *
 * 为什么非有这个不可：指数卡上只有「C = −0.3239」一个数，
 * 评委问「这 6.48 分是哪些记录构成的」，屏幕上没有答案。
 * 下钻里每一条都带期间、指标、原文出处与判定理由，答的就是这个问题。
 *
 * ⚠ 明细由**后端**算（`/narrative/index/components/{key}`），而且与指数
 * 走同一次判定。前端只渲染，不重算、不汇总。
 */
function ComponentTable({
  data,
  projectId,
}: {
  data: NarrativeIndex
  projectId: string
}) {
  const [open, setOpen] = useState<string | null>(null)
  return (
    <>
      <table className="plain-table component-table">
        <thead>
          <tr>
            <th>分项</th>
            <th>取值域</th>
            <th className="num">取值</th>
            <th>含义</th>
          </tr>
        </thead>
        <tbody>
          {COMPONENT_ROWS.map((row) => {
            const value = data.components[row.key]
            const on = open === row.drill
            return (
              <tr
                key={row.key}
                className={on ? 'row-open' : undefined}
                onClick={() => setOpen(on ? null : row.drill)}
                title="点开看这一项由哪些记录构成"
              >
                <td>
                  <span className="caret">{on ? '▾' : '▸'}</span>
                  {row.label}
                </td>
                <td className="range">{row.range}</td>
                <td className="num">
                  {/* 未核验时显示「未核验」而不是 0——0 会被读成「算出来是零」 */}
                  {value === null ? <span className="muted">未核验</span> : value}
                </td>
                <td className="msg">{row.meaning}</td>
              </tr>
            )
          })}
        </tbody>
      </table>

      {open && (
        <ComponentDrilldown
          projectId={projectId}
          drill={open}
          onClose={() => setOpen(null)}
        />
      )}
    </>
  )
}

/**
 * 一个分项的构成明细。按需取数——**展开才请求**。
 * 一进站就拉五个分项的话，C 那一项有 71 条、P 有 121 条，页面白等。
 */
function ComponentDrilldown({
  projectId,
  drill,
  onClose,
}: {
  projectId: string
  drill: string
  onClose: () => void
}) {
  const detail = useApi<ComponentDetail>(
    `/projects/${projectId}/narrative/index/components/${drill}`,
  )
  const d = detail.data

  return (
    <section className="card drilldown">
      <header className="drilldown-head">
        <h3>{d?.label_cn ?? '载入中…'}</h3>
        <span className="subtitle">
          {d ? `共 ${d.total} 条` : ''}
          {d?.truncated ? '（已截断）' : ''}
        </span>
        <button type="button" className="link-btn" onClick={onClose}>
          收起
        </button>
      </header>

      <AsyncBoundary
        loading={detail.loading}
        error={detail.error}
        onRetry={detail.reload}
      >
        {d?.note && <p className="drilldown-note">{d.note}</p>}
        <ul className="drilldown-list">
          {d?.rows.map((r, i) => (
            <li key={i} className={`kind-${r.kind}`}>
              <div className="row-head">
                <span className={`contrib c-${r.contribution}`}>
                  {r.contribution}
                </span>
                <span className="row-label">{r.label}</span>
              </div>
              <div className="row-meta">
                {r.period && <span>期间 {r.period}</span>}
                {r.report_period && <span>报告年 {r.report_period}</span>}
                {r.metric_label && <span>判据 {r.metric_label}</span>}
                {r.source_page != null && <span>年报第 {r.source_page} 页</span>}
                {r.reviewer && <span>复核 {r.reviewer}</span>}
              </div>
              {r.detail && <div className="row-detail">{r.detail}</div>}
              {/* 算式与代入的数：**逐条给**，这正是「点结论回到计算过程」 */}
              {r.formula && <div className="row-formula mono">{r.formula}</div>}
              {r.inputs.length > 0 && (
                <div className="row-inputs mono">
                  {r.inputs.map((x, j) => (
                    <div key={j}>{x}</div>
                  ))}
                </div>
              )}
              {r.source_text && (
                <blockquote className="row-source">{r.source_text}</blockquote>
              )}
            </li>
          ))}
        </ul>
      </AsyncBoundary>
    </section>
  )
}

// ---------------------------------------------------------------- 对照表

/**
 * 横向柱状图：一行一个类目，条长按**后端给的计数**换算。
 *
 * ⚠ **条长是「呈现」不是「计算」。** 条右端标的数字是后端原样给的绝对条数，
 * 没有任何财务数字在浏览器里被算出来。这是仓库里已有的判据：
 * `format.ts` 允许把 0.0742 显示成 7.42%，理由是「只是把同一张脸换成另一种
 * 写法，不产生新信息」。
 *
 * ⚠ 红线：**不许把比例打成文字**。写成「相悖 27.9%」的那一刻，它就成了
 * 一个前端算出来、还会被评审引用进材料里的新数字——性质立刻变了。
 */
function Bars({
  rows,
}: {
  rows: { label: string; n: number; color: string }[]
}) {
  const max = Math.max(1, ...rows.map((r) => r.n))
  return (
    <div className="bars">
      {rows.map((r) => (
        <div className="bar-row" key={r.label}>
          <span className="bar-label">{r.label}</span>
          <span className="bar-track">
            <i style={{ width: `${(r.n / max) * 100}%`, background: r.color }} />
          </span>
          <span className="bar-n">{r.n}</span>
        </div>
      ))}
    </div>
  )
}

/**
 * 判定分布。与下面那张表是同一批数据：柱状看绝对量，表看逐条。
 *
 * ⚠ **没数据时要说明，不能 `return null`。** 整节静默消失的话，
 * 看的人分不出「还没跑判定」和「跑了但没有判定结果」——
 * 而这两件事一个要跑脚本、一个是解析出了问题。
 */
function VerdictBars({ counts }: { counts: Record<string, number> | null }) {
  if (!counts) {
    return (
      <section className="panel">
        <h3>判定分布</h3>
        <p className="empty-note">这次分析还没有生成判定结果。</p>
      </section>
    )
  }

  const order: Verdict[] = [
    'unverifiable', 'contradicted', 'supported', 'needs_review', 'neutral',
    'incomparable',
  ]
  // ⚠ `counts` 里**不含取值为 0 的 verdict**（后端是 GROUP BY 出来的），
  // 所以一律 `?? 0`。少了兜底的话，某种判定整行消失，而页面上看起来
  // 「就是这几种」——一个不报错的错。
  const rows = order
    .filter((v) => (counts[v] ?? 0) > 0)
    .map((v) => ({
      label: VERDICT[v].label,
      n: counts[v] ?? 0,
      color: VERDICT[v].color,
    }))

  const total = rows.reduce((sum, r) => sum + r.n, 0)
  if (!rows.length) {
    // 走得到这里说明 `counts` 存在但全是 0（后端 GROUP BY 出来的，
    // 没有判定就一个键都没有）。**照样要说一句**，
    // 不然这一节仍然是「静默消失」——刚修的正是这个毛病。
    return (
      <section className="panel">
        <h3>判定分布</h3>
        <p className="empty-note">这次分析还没有生成判定结果。</p>
      </section>
    )
  }

  return (
    <section className="panel">
      <h3>
        判定分布
        <span className="subtitle">
          共 {total} 条 · 条长按条数，颜色与下方判定表一致
        </span>
      </h3>
      <Bars rows={rows} />
      <div style={{ marginTop: 12 }}>
        <div className="dist-bar">
          {order
            .filter((v) => (counts[v] ?? 0) > 0)
            .map((v) => (
              // 段宽由 flex-grow 按原始条数分配——前端一行算术都没写
              <i
                key={v}
                style={
                  {
                    ['--n' as string]: counts[v] ?? 0,
                    ['--c' as string]: VERDICT[v].color,
                  } as React.CSSProperties
                }
                title={`${VERDICT[v].label} ${counts[v] ?? 0}`}
              />
            ))}
        </div>
        <p className="hint" style={{ marginTop: 6 }}>
          上面是逐类目的绝对条数，这一条是同一批数据的堆叠视图（看占比）。
          点下方判定表的图例可以筛选。
        </p>
      </div>
    </section>
  )
}

/**
 * 主张主题分布。数据是后端 `GROUP BY claim_type` 算好的计数。
 *
 * ⚠ 同上： `return null` 会让整节静默消失，分不出「没抽过」和「抽出来是空的」。
 */
function ThemeBars({ stats }: { stats: ClaimsResponse['stats'] | null }) {
  if (!stats) {
    return (
      <section className="panel">
        <h3>主张主题分布</h3>
        <p className="empty-note">这次分析还没有抽到主张。</p>
      </section>
    )
  }

  const rows = Object.entries(stats.by_type ?? {})
    .sort((a, b) => b[1] - a[1])
    // 主题没有语义色——它们不是判定结果，用同一族中性蓝即可。
    // ⚠ 别给主题配红绿：那会和判定的语义色撞成第三个体系。
    .map(([type, n]) => ({
      label: CLAIM_TYPE_LABELS[type] ?? type,
      n,
      color: '#4a7fd4',
    }))

  if (!rows.length) {
    // 同 VerdictBars：`stats` 在但 `by_type` 空，也**不能说消失就消失**。
    return (
      <section className="panel">
        <h3>主张主题分布</h3>
        <p className="empty-note">这次分析还没有抽到主张。</p>
      </section>
    )
  }

  return (
    <section className="panel">
      <h3>
        主张主题分布
        <span className="subtitle">
          共 {stats.total} 条主张 · 主题取自字段字典的判据表
        </span>
      </h3>
      <Bars rows={rows} />
    </section>
  )
}

/** 主题的中文名。后端给的是键，页面要给人看。 */
const CLAIM_TYPE_LABELS: Record<string, string> = {
  demand: '需求与产销',
  order: '订单',
  capacity: '产能与项目',
  collection: '回款与应收',
  product_mix: '产品结构',
  cost: '降本增效',
  risk: '风险与环保',
  macro: '宏观与行业',
  other: '其他',
}

function VerdictFilter({
  counts,
  value,
  onChange,
}: {
  counts: Record<string, number>
  value: Verdict | 'all'
  onChange: (v: Verdict | 'all') => void
}) {
  const order: Verdict[] = [
    'supported', 'contradicted', 'neutral', 'needs_review', 'unverifiable',
    'incomparable',
  ]
  const total = order.reduce((sum, v) => sum + (counts[v] ?? 0), 0)
  return (
    <div className="verdict-filter">
      <button
        type="button"
        className={value === 'all' ? 'active' : ''}
        onClick={() => onChange('all')}
      >
        全部 {total}
      </button>
      {order
        .filter((v) => (counts[v] ?? 0) > 0)
        .map((v) => (
          <button
            key={v}
            type="button"
            className={value === v ? 'active' : ''}
            style={value === v ? { borderColor: VERDICT[v].color } : undefined}
            onClick={() => onChange(v)}
          >
            <span style={{ color: VERDICT[v].color }}>●</span> {VERDICT[v].label}{' '}
            {counts[v]}
          </button>
        ))}
    </div>
  )
}

function MatchTable({
  rows,
  onOpenFact,
}: {
  rows: ClaimMatch[]
  onOpenFact: (factId: string) => void
}) {
  return (
    <table className="plain-table match-table">
      <thead>
        <tr>
          <th>判定</th>
          <th>期间</th>
          <th>主题</th>
          <th>主张原文</th>
          <th>主判据</th>
          <th>理由</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.match_id}>
            <td>
              <span className="chip" style={{ color: VERDICT[r.verdict].color }}>
                ● {VERDICT[r.verdict].label}
              </span>
            </td>
            <td className="nowrap">{r.claim_period}</td>
            <td className="nowrap">{r.claim_type}</td>
            <td className="claim-text" title={r.claim_text}>
              {r.claim_text}
            </td>
            <td className="nowrap">{r.metric_key || '—'}</td>
            <td className="msg">
              {r.reason}
              <PlanVariance row={r} />
            </td>
            <td>
              {r.inputs && <FactLinks inputs={r.inputs} onOpen={onOpenFact} />}
              {!r.inputs && (
                <button
                  type="button"
                  className="link-button"
                  onClick={() => onOpenFact(`__page__${r.source_page}`)}
                  disabled
                  title="本条判定没有引用事实（未披露直接指标）"
                >
                  无依据
                </button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/**
 * 绝对量目标的**单位换算留痕**与「原始计划偏差」。
 *
 * 目标原样写着「亿元」而事实库存的是「百万元」，这一步换算必须摆出来：
 * 只给一个换算过的数，用户没法核对，而「计算可复算」正是本系统的卖点。
 * 后端同时返回原始单位、换算因子与标准化数值（会计口径 8-1 点名要留的四样）。
 *
 * ⚠ **这里只排版，不做任何计算。** 三个数都是后端给的：
 * 目标 × 因子 = 标准化值，以及偏差。前端算出来的数没法审计。
 * ⚠ 偏差**不计分**（8-3 第 4 条）——所以旁边那句「不计分」不是装饰，
 * 少了它，用户会以为这一行在为指数贡献分数。
 */
function PlanVariance({ row }: { row: ClaimMatch }) {
  if (!row.target_millions || !row.unit_factor) return null
  const target = row.magnitude_target ?? ''
  const variance = row.plan_variance
  const sign = variance && !variance.startsWith('-') ? '+' : ''
  return (
    <div className="plan-variance">
      <span className="pv-step">
        {target} × {row.unit_factor} = {groupDigits(row.target_millions)} 百万元
      </span>
      {variance && (
        <span className="pv-gap">
          原始计划偏差 {sign}
          {groupDigits(variance)} 百万元
          <span className="pv-muted">（不计分）</span>
        </span>
      )}
      {row.plan_reference && <div className="pv-ref">{row.plan_reference}</div>}
    </div>
  )
}

/** 从 `inputs` 的 JSON 里取出 fact_id，做成可点的入口。 */
function FactLinks({
  inputs,
  onOpen,
}: {
  inputs: string
  onOpen: (factId: string) => void
}) {
  let ids: string[] = []
  try {
    const parsed = JSON.parse(inputs)
    ids = Array.isArray(parsed?.fact_ids) ? parsed.fact_ids : []
  } catch {
    ids = []
  }
  if (ids.length === 0) return <span className="muted">—</span>
  return (
    <span className="fact-links">
      {ids.map((id, i) => (
        <button key={id} type="button" className="link-button" onClick={() => onOpen(id)}>
          证据{i + 1}
        </button>
      ))}
    </span>
  )
}

// ---------------------------------------------------------------- 抽取法

/**
 * 本次分析用了哪种抽取法、抽出来多少。
 *
 * ⚠ **只有一种抽取法时也必须显示出来，而且要说明为什么只有一种。**
 * 原来写的是 `if (rows.length < 2) return null` —— 整节**静默消失**。
 * 后果是：页面上没有这一块，看的人分不出「这块还没做」和
 * 「这块本来就没有内容」。实测会计同学问的就是这句「怎么啥也没有」。
 *
 * 空白和「没有数据」长得一模一样，而这里两者该说的话完全不同：
 * 一种是「模型法还没跑」（可以跑），一种是「跑了但一条都没抽出来」（有问题）。
 *
 * 现在**只跑规则法**（本届只用规则法，见 CLAUDE.md），所以这里就是
 * 一行说明加一张统计表，不再摆出「左右对照」的架势——没有第二种方法时，
 * 那张对照表除了让人以为我们少跑了一半，没有别的用处。
 * 两种方法并在时它自动恢复成对照表，不用改代码。
 */
function ExtractorComparison({ rows }: { rows?: ExtractorSummary[] }) {
  // ⚠ **必须容忍 undefined**：后端可能是旧版本、字段可能还没上。
  // 崩溃的代价是整页白屏（实测发生过一次），而这块内容只是页面的一节。
  // 少一节远好过整页打不开——尤其是演示的时候。
  if (!rows || rows.length === 0) {
    return <p className="empty-note">这次分析还没有抽取结果。</p>
  }

  const hasLlm = rows.some((r) => r.extractor.startsWith('llm:'))
  const hasRule = rows.some((r) => r.extractor.startsWith('rule:'))
  const both = hasLlm && hasRule

  return (
    <details className="extractor-compare" open>
      <summary>{both ? '两种抽取法' : '抽取法'}</summary>

      <p className="empty-note">
        {both ? (
          <>
            两种抽取法**并存、不互相覆盖**——同一句话两边都抽到时会有两条。
            条数少不代表差、也不代表好；「哪个更准」需要人看着原文判，
            系统不做裁定。
          </>
        ) : hasRule ? (
          <>
            本次分析使用<b>规则法</b>抽取：确定性、可复算，同输入必得同输出——
            每一个数字都能从年报原文重算一遍，这是本系统「结论可追溯」的前提。
          </>
        ) : (
          <>本次分析用的是模型法抽取。</>
        )}
      </p>

      <table className="plain-table">
        <thead>
          <tr>
            <th>抽取法</th>
            <th className="num">条数</th>
            <th className="num">可验证</th>
            <th className="num">可验证占比</th>
            <th className="num">主题数</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.extractor}>
              <td className="extractor-name" title={r.extractor}>
                {labelOf(r.extractor)}
              </td>
              <td className="num">{r.total}</td>
              <td className="num">{r.verifiable}</td>
              <td className="num">
                {r.total > 0 ? `${Math.round((r.verifiable / r.total) * 100)}%` : '—'}
              </td>
              <td className="num">{r.theme_count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  )
}

function labelOf(extractor: string): string {
  if (extractor.startsWith('rule:')) return `规则法（${extractor.slice(5)}）`
  if (extractor.startsWith('llm:')) return `模型（${extractor.slice(4)}）`
  return extractor
}

// ---------------------------------------------------------------- 未判定

function BackgroundClaims({ data }: { data: ClaimsResponse }) {
  const rows = data.claims.filter((c) => c.background_only === 1)
  if (rows.length === 0) {
    return <div className="async-state async-empty">没有被排除的主张。</div>
  }
  // 同一主题的禁止推断只展示一次，避免刷屏
  const forbidden = new Set<string>()
  for (const r of rows) {
    for (const f of r.forbidden_simplifications) forbidden.add(f)
  }

  return (
    <>
      <p className="page-note">
        以下 {rows.length} 条主张**不进入一致性评分**。它们的主题在主判据表里
        没有对应的直接指标，按会计口径 v1.1 不降级用代理指标硬判——
        那会给出「看起来有结论、实际没证据」的判断。
      </p>
      <ul className="background-list">
        {rows.slice(0, 12).map((r) => (
          <li key={r.claim_id}>
            <span className="tag">{r.claim_type}</span>
            <span className="claim-text">{r.claim_text}</span>
            <span className="muted">第 {r.source_page} 页</span>
          </li>
        ))}
      </ul>
      {rows.length > 12 && (
        <p className="hint">（仅显示前 12 条，共 {rows.length} 条）</p>
      )}
      {forbidden.size > 0 && (
        <details className="forbidden-box">
          <summary>这些主题被禁止的简化推断（会计口径 v1.1 §A.6）</summary>
          <ul>
            {[...forbidden].map((f, i) => (
              <li key={i}>{f}</li>
            ))}
          </ul>
        </details>
      )}
    </>
  )
}

// ---------------------------------------------------------------- 勾稽

function ChecksView({ data }: { data: ChecksResponse }) {
  const failed = data.results.filter((r) => r.status === 'failed')

  return (
    <>
      {/* 覆盖率和结论必须一起显示：只说「通过」会把「大部分根本没查」
          读成「什么都对得上」 */}
      <div className="check-summary">
        <div className="summary-line">{data.summary.coverage_line}</div>
        <div className="summary-tiles">
          <Tile label="可评估" value={data.summary.evaluable} />
          <Tile label="通过" value={data.summary.passed} tone="ok" />
          <Tile
            label="报表不平"
            value={data.summary.hard_failed}
            tone={data.summary.hard_failed ? 'bad' : undefined}
          />
          <Tile
            label="存疑"
            value={data.summary.soft_failed}
            tone={data.summary.soft_failed ? 'warn' : undefined}
          />
          <Tile label="缺数据" value={data.summary.skipped_missing_data} />
        </div>
        <div className="sheet-verdict">
          报表本身是平的：<strong>{data.summary.sheet_ok ? '是' : '否'}</strong>
          {data.summary.soft_failed > 0 && (
            <span className="hint">（「存疑」多为字段字典缺字段，不是报表有问题）</span>
          )}
        </div>
      </div>

      {data.warnings.length > 0 && (
        <div className="warn-box">
          <strong>取数阶段的问题</strong>
          <ul>
            {data.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="check-failures">
        {failed.map((r) => (
          <FailureCard key={`${r.rule_key}-${r.period}`} result={r} />
        ))}
      </div>

      <details>
        <summary>逐条结果（{data.results.length} 项）</summary>
        <table className="plain-table">
          <thead>
            <tr>
              <th>期间</th>
              <th>规则</th>
              <th>结论</th>
              <th className="num">差额</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {data.results.map((r) => (
              <tr key={`${r.rule_key}-${r.period}`}>
                <td>{r.period}</td>
                <td title={r.formula}>{r.rule_key}</td>
                <td>
                  <StatusChip status={r.status} severity={r.severity} />
                </td>
                <td className="num">{r.diff ? groupDigits(r.diff) : '—'}</td>
                <td className="msg">{r.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </>
  )
}

function Tile({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone?: 'ok' | 'warn' | 'bad'
}) {
  return (
    <div className={`tile${tone ? ` tile-${tone}` : ''}`}>
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
    </div>
  )
}

function StatusChip({
  status,
  severity,
}: {
  status: ChecksResponse['results'][number]['status']
  severity: ChecksResponse['results'][number]['severity']
}) {
  const meta = CHECK[status]
  const sev = severity !== 'info' ? SEVERITY[severity] : null
  return (
    <span className="chip" style={{ color: meta.color }}>
      {meta.label}
      {status === 'failed' && sev ? `（${sev.label}）` : ''}
    </span>
  )
}

function FailureCard({ result }: { result: ChecksResponse['results'][number] }) {
  return (
    <div className="failure-card">
      <div className="failure-head">
        <strong>
          {result.period} 年 · {result.rule_key}
        </strong>
        <span style={{ color: SEVERITY[result.severity].color }}>
          {SEVERITY[result.severity].label}
        </span>
      </div>
      <div className="failure-body">{result.message}</div>
      <dl className="kv">
        <div className="kv-row">
          <dt>公式</dt>
          <dd>{result.formula}</dd>
        </div>
        <div className="kv-row">
          <dt>左边</dt>
          <dd>{result.lhs ? groupDigits(result.lhs) : '—'}</dd>
        </div>
        <div className="kv-row">
          <dt>右边</dt>
          <dd>{result.rhs ? groupDigits(result.rhs) : '—'}</dd>
        </div>
        <div className="kv-row">
          <dt>差额</dt>
          <dd>{result.diff ? groupDigits(result.diff) : '—'}</dd>
        </div>
      </dl>
      {result.suggestion && (
        <div className="failure-suggestion">建议：{result.suggestion}</div>
      )}
    </div>
  )
}
