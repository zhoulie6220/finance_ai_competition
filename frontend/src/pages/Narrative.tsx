import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'

import { useApi } from '../api/client'
import type { CheckResult, CheckSummary, ChecksResponse } from '../api/types'
import AsyncBoundary from '../components/AsyncBoundary'
import EvidenceDrawer from '../components/EvidenceDrawer'
import { CHECK, SEVERITY } from '../theme/colors'
import { groupDigits } from '../format'

/**
 * 叙事一致性诊断。
 *
 * ## 为什么这一页现在没有分数
 *
 * 诊断指数要先有「主张」才有得算：H（历史兑现度）与 C（当期一致性）分别
 * 是历史主张与本期主张的判定平均值。而主张抽取还没跑，库里 claim / claim_match
 * 都是 0 行。
 *
 * 页面**如实显示「证据不足，不出分」**，不显示 0 分也不显示 50 分——
 * 会计口径 v1.1 §A.4 明确要求「不用 0 分或 50 分代替」。理由很实际：
 * 页面上出现一个大号数字，就不会有人去看人工复核入口了。
 *
 * ## 这一页上有什么是真的
 *
 * 下面「财务质量项（Q）的输入」是**真实的勾稽校验结果**——诊断指数的 Q 项
 * 就是由它喂的。44 项期间校验、21 项可评估，其中 8 项存疑。
 */

export default function Narrative() {
  const { projectId } = useParams<{ projectId: string }>()
  const [factId, setFactId] = useState<string | null>(null)

  const checks = useApi<ChecksResponse>(
    projectId ? `/checks` : null,
    useMemo(() => ({ project_id: projectId }), [projectId]),
  )

  return (
    <div className="page">
      <header className="page-head">
        <h2>叙事一致性诊断</h2>
        <p className="page-note">
          系统把管理层在年报里的表述拆成可验证主张，与后续财务事实交叉验证。
          指数是**规则型研究工具**，不代表管理层诚信、欺诈概率或股票收益。
        </p>
      </header>

      <IndexCard projectId={projectId ?? ''} />

      <section className="panel">
        <h3>
          财务质量项（Q）的输入 —— 三表勾稽校验
          <span className="subtitle">
            诊断指数的 Q 项由此项产生
          </span>
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

function IndexCard({ projectId }: { projectId: string }) {
  // 诊断运行表里还没有记录——指数从未跑过。
  // 这里刻意**不造假数据**，如实说明缺什么、怎么补。
  return (
    <section className="panel index-card">
      <div className="index-score index-insufficient">
        <div className="score-value">不出分</div>
        <div className="score-grade">证据不足</div>
      </div>

      <div className="index-reason">
        <h3>为什么不出分</h3>
        <ul>
          <li>
            系统尚未从 {projectId ? '本项目' : '该项目'}的 MD&amp;A 中抽取可验证主张，
            因此 H（历史兑现度）与 C（当期一致性）都没有观测。
          </li>
          <li>
            闸门要求 n/N ≥ 0.60、n ≥ 5、<strong>H 与 C 各至少一条</strong>、
            且 R/P/Q 均可算并核验完成。任一不满足就不出分。
          </li>
          <li>
            证据不足时**不用 0 分或 50 分代替**。页面上出现一个大号数字，
            人工复核的入口就没人看了。
          </li>
        </ul>
        <p className="hint">
          补齐路径：跑主张抽取 → 匹配财务事实 → 计算指数。在那之前，
          下面这一节的勾稽校验是真实的，可以用。
        </p>
      </div>
    </section>
  )
}

// ---------------------------------------------------------------- 勾稽结果

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
            <span className="hint">
              （「存疑」多为字段字典缺字段，不是报表有问题）
            </span>
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

      {failed.length > 0 && (
        <div className="check-failures">
          {failed.map((r) => (
            <FailureCard key={`${r.rule_key}-${r.period}`} result={r} />
          ))}
        </div>
      )}

      <h4>逐条结果</h4>
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
      {/* 下一步：从校验结论点回「参与计算的那几笔事实」。
          input_facts 已经随结果落库（fact_check_result.input_facts），
          接上跳转入口即可，不需要改后端。 */}
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
  status: CheckResult['status']
  severity: CheckResult['severity']
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

function FailureCard({ result }: { result: CheckResult }) {
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
        <div className="kv-row">
          <dt>容差</dt>
          <dd>{result.tolerance ? groupDigits(result.tolerance) : '—'}</dd>
        </div>
      </dl>
      {result.suggestion && (
        <div className="failure-suggestion">建议：{result.suggestion}</div>
      )}
    </div>
  )
}

/** 供其它页面复用。 */
export type { CheckSummary }
