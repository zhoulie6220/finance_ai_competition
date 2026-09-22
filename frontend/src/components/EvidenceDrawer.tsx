/**
 * 证据面板：点开一个数字，看到它是怎么来的。
 *
 * **这是整个系统区别于「AI 财报摘要工具」的地方**，所以它显示的不是一句
 * 「数据来源：年报」，而是四样能核对的东西：
 *
 *   1. 算出这个数的公式（后端引擎给的 `formula`，不是前端拼的）
 *   2. 代进公式的入参
 *   3. 每个入参**来自哪份年报的第几页哪张表**
 *   4. 那一行的**原文**——照抄，不做任何加工
 *
 * 第 4 条最要紧。原文在这里被原样展示，包括它是正数还是负数、有没有「其中：」
 * 前缀。一旦这里显示的是加工过的文字，评审看到的就成了我们说的话，
 * 而不是年报说的话。
 */

import type { EvidenceSource } from '../types/view'

export interface EvidenceTarget {
  /** 例如「2024 年 · 营业收入」 */
  title: string
  /** 已经格式化好的数值，例如「322,115.85 百万元」 */
  value: string
  /** 后端引擎给的公式。前端不拼公式。 */
  formula?: string | null
  inputs?: unknown
  sources: EvidenceSource[]
  /** 拒绝出数的理由，或重述说明。有值时要显著显示。 */
  note?: string | null
  /** 这个数是程序算出来的（年报里没有这一行），而不是读到的 */
  derived?: boolean
}

interface Props {
  target: EvidenceTarget | null
  onClose: () => void
}

/** 入参表：`{2014: "187414.5", ...}` → 一行行列出来。 */
function Inputs({ inputs }: { inputs: unknown }) {
  if (inputs === null || inputs === undefined) return null
  const entries =
    typeof inputs === 'object' && !Array.isArray(inputs)
      ? Object.entries(inputs as Record<string, unknown>)
      : null
  if (!entries?.length) return null

  return (
    <div className="ev-block">
      <h4>代入公式的数</h4>
      <dl className="ev-inputs">
        {entries.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

export function EvidenceDrawer({ target, onClose }: Props) {
  if (!target) return null

  return (
    <>
      <div className="ev-mask" onClick={onClose} />
      <aside className="ev-drawer" role="dialog" aria-label="证据">
        <header className="ev-head">
          <div>
            <p className="ev-eyebrow">证据</p>
            <h3>{target.title}</h3>
          </div>
          <button className="ev-close" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </header>

        <p className="ev-value">{target.value}</p>

        {target.derived && (
          <p className="hint">
            年报里<b>没有</b>这一行，它是程序按下面的公式算出来的。
          </p>
        )}

        {target.note && <p className="hint warn-box">{target.note}</p>}

        {target.formula && (
          <div className="ev-block">
            <h4>公式</h4>
            {/* 公式来自后端引擎的 formula 字段，前端只负责显示 */}
            <code className="ev-formula">{target.formula}</code>
          </div>
        )}

        <Inputs inputs={target.inputs} />

        <div className="ev-block">
          <h4>
            年报出处
            {target.sources.length > 1 && <span className="ev-count">共 {target.sources.length} 处</span>}
          </h4>

          {!target.sources.length && (
            <p className="empty">这一项没有可展示的出处。</p>
          )}

          {target.sources.map((s, i) => (
            <div key={s.fact_id ?? i} className="ev-src">
              {s.role && <p className="ev-role">{s.role}</p>}

              <p className="ev-file">
                {s.source_file ?? '（未记录文件）'}
                {s.source_page != null && <b>第 {s.source_page} 页</b>}
                {s.source_table && <span className="ev-table">{s.source_table}</span>}
              </p>

              {/* 原文照抄。`white-space: pre-wrap` 保留解析时补的列间距，
                  读起来才像年报上那一行。 */}
              {s.source_text ? (
                <pre className="ev-text">{s.source_text}</pre>
              ) : (
                <p className="empty">这一处没有留存原文。</p>
              )}

              {s.fact_id && <p className="ev-id">事实编号 {s.fact_id}</p>}
            </div>
          ))}
        </div>
      </aside>
    </>
  )
}
