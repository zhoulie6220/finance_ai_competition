import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'

import { useApi } from '../api/client'
import type {
  ChecksResponse,
  ClaimMatch,
  ClaimsResponse,
  MatchesResponse,
  NarrativeIndex,
  Verdict,
} from '../api/types'
import AsyncBoundary from '../components/AsyncBoundary'
import EvidenceDrawer from '../components/EvidenceDrawer'
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
  const claims = useApi<ClaimsResponse>(
    projectId ? `/projects/${projectId}/narrative/claims` : null,
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
        {index.data && <IndexCard data={index.data} />}
      </AsyncBoundary>

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
          emptyText={matches.data?.hint ?? '还没有判定结果。'}
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

      <EvidenceDrawer factId={factId} onClose={() => setFactId(null)} />
    </div>
  )
}

// ---------------------------------------------------------------- 指数卡片

function IndexCard({ data }: { data: NarrativeIndex }) {
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

        <ComponentTable data={data} />

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
          <div className="hint">{data.user_hint}</div>
        </div>

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
  label: string
  range: string
  meaning: string
}> = [
  { key: 'history', label: 'H 历史兑现度', range: '[−1, 1]', meaning: '以前报告的前瞻主张，用后来的实际结果验证' },
  { key: 'current', label: 'C 当期一致性', range: '[−1, 1]', meaning: '本期主张与同期财务事实的匹配程度' },
  { key: 'risk', label: 'R 风险披露充分度', range: '[0, 1]', meaning: '四项风险检查中说明了对象、路径与依据的占比' },
  { key: 'template', label: 'P 缺乏可验证性', range: '[0, 1]', meaning: '缺乏可验证对象、期间或结果的实质表述占比' },
  { key: 'quality', label: 'Q 财务质量冲突', range: '[0, 1]', meaning: '已确认触发的质量检查占比' },
]

function ComponentTable({ data }: { data: NarrativeIndex }) {
  return (
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
          return (
            <tr key={row.key}>
              <td>{row.label}</td>
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
  )
}

// ---------------------------------------------------------------- 对照表

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
            <td className="msg">{r.reason}</td>
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
