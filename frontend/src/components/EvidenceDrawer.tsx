import { useEffect, useMemo, useRef, useState } from 'react'

import { apiGet, useApi } from '../api/client'
import type { FactDetail, PageDetail } from '../api/types'
import {
  basename,
  formatMoney,
  formatPage,
  scopeLabel,
} from '../format'
import AsyncBoundary from './AsyncBoundary'

/**
 * 证据抽屉：把一笔事实变成「文件 → 页码 → 原文」。
 *
 * 这是整个项目最重要的一块 UI，因为赛事要求的「过程可追溯」最终就落在它身上——
 * 任何结论都要能点回原文。docs/03 把这条写成了「证据链与任务时间线绝不砍」。
 *
 * 三条设计约束：
 *   1. **原文用抽取出来的正文，不用 PDF 切片。** 样例 PDF 有 94MB 且被
 *      .gitignore 挡在仓库外，评委 clone 下来根本没有那些文件；而正文在库里、
 *      能全文检索、能高亮，比 PDF 更好用。
 *   2. **命中的那句要高亮。** 一页 1600 字，不高亮的话人得自己找。
 *   3. **把原始值也显示出来。** 库里的 value_millions 是换算过单位的，
 *      而人在年报上看到的是「21,385,905,275.51」。两个都给，才核对得上。
 */

interface Props {
  factId: string | null
  onClose: () => void
}

export default function EvidenceDrawer({ factId, onClose }: Props) {
  const fact = useApi<FactDetail>(factId ? `/facts/${factId}` : null)
  const [page, setPage] = useState<PageDetail | null>(null)
  const [pageError, setPageError] = useState<string | null>(null)
  const [pageLoading, setPageLoading] = useState(false)

  // 事实一变就把上一页清掉，否则会短暂显示上一条事实的原文——
  // 那一瞬间看起来像是「新事实引用了一页不相关的原文」。
  useEffect(() => {
    setPage(null)
    setPageError(null)
  }, [factId])

  useEffect(() => {
    if (!factId) return
    let cancelled = false
    setPageLoading(true)
    apiGet<PageDetail>(`/facts/${factId}/page`)
      .then((body) => {
        if (!cancelled) setPage(body)
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setPageError(err instanceof Error ? err.message : String(err))
        }
      })
      .finally(() => {
        if (!cancelled) setPageLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [factId])

  if (!factId) return null

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside
        className="drawer"
        onClick={(e) => e.stopPropagation()}
        aria-label="证据"
      >
        <header className="drawer-head">
          <h3>证据链</h3>
          <button type="button" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </header>

        <AsyncBoundary
          loading={fact.loading}
          error={fact.error}
          onRetry={fact.reload}
        >
          {fact.data && (
            <>
              <FactFields fact={fact.data} />

              <AsyncBoundary
                loading={pageLoading}
                error={pageError}
                empty={!page && !pageLoading}
                emptyText="这笔事实定位不到原文页。"
              >
                {page && (
                  <PageView
                    page={page}
                    highlight={fact.data?.source_text ?? null}
                    onNavigate={(pageId) => {
                      setPageLoading(true)
                      apiGet<PageDetail>(`/pages/${pageId}`)
                        .then(setPage)
                        .catch((e: unknown) =>
                          setPageError(e instanceof Error ? e.message : String(e)),
                        )
                        .finally(() => setPageLoading(false))
                    }}
                  />
                )}
              </AsyncBoundary>
            </>
          )}
        </AsyncBoundary>
      </aside>
    </div>
  )
}

// ---------------------------------------------------------------- 事实字段

function FactFields({ fact }: { fact: FactDetail }) {
  return (
    <section className="drawer-section">
      <div className="fact-headline">
        <span className="fact-label">{fact.metric}</span>
        <span className="fact-value">
          {formatMoney(fact.value, fact.unit)}
        </span>
      </div>

      <dl className="kv">
        <Row label="口径" value={scopeLabel(fact.scope)} />
        <Row label="期间" value={`${fact.period}（${fact.period_kind}）`} />
        <Row label="状态" value={fact.status} />
        <Row label="置信度" value={fact.confidence.toFixed(2)} />
        {!fact.comparable && (
          <Row
            label="不可比"
            value={`是 —— ${fact.incomparable_reason ?? '未注明原因'}`}
            warn
          />
        )}
        {fact.restated && <Row label="重述" value="是（原值另存一行）" warn />}
        <Row label="抽取方式" value={fact.extractor} />
      </dl>

      <h4>原始披露</h4>
      <dl className="kv">
        {/* 换算过程必须可核对：人在年报上看到的是原始值和原始单位 */}
        <Row label="原始值" value={fact.value_raw ?? '—'} />
        <Row label="原始单位" value={fact.raw_unit ?? '—'} />
        <Row label="换算系数" value={fact.unit_factor ?? '—'} />
        <Row label="表内行名" value={fact.source_row_label ?? '—'} />
        <Row label="所在表" value={fact.source_table ?? '—'} />
      </dl>

      {fact.sign_basis && (
        <>
          <h4>列报符号</h4>
          <dl className="kv">
            <Row label="符号依据" value={fact.sign_basis} />
          </dl>
        </>
      )}
    </section>
  )
}

function Row({
  label,
  value,
  warn = false,
}: {
  label: string
  value: string
  warn?: boolean
}) {
  return (
    <div className={warn ? 'kv-row kv-warn' : 'kv-row'}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}

// ---------------------------------------------------------------- 原文

function PageView({
  page,
  highlight,
  onNavigate,
}: {
  page: PageDetail
  /** 这笔事实的原文片段。命中处要高亮——一页一千多字，不高亮得自己找。 */
  highlight: string | null
  onNavigate: (pageId: string) => void
}) {
  const parts = useMemo(
    () => splitByHighlight(page.text ?? '', highlight),
    [page.text, highlight],
  )

  const markRef = useRef<HTMLElement | null>(null)

  // ★ 打开抽屉就**自动滚到命中的那句**。
  //
  // 没有这一步的话，「点击结论回到原文」只是把原文**打开**了，
  // 但命中处在屏幕外面（实测第 63 页的利润表在第 1269 字，
  // 而页首是资产负债表的尾部）。看的人得自己往下翻着找——
  // 那就等于「回到原文」这件事没做完。
  useEffect(() => {
    if (!parts.matched) return
    // 等一帧，确保 mark 已经渲染出来
    const id = window.requestAnimationFrame(() => {
      markRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    })
    return () => window.cancelAnimationFrame(id)
  }, [page.page_id, parts.matched])

  return (
    <section className="drawer-section">
      <h4>年报原文</h4>
      <div className="page-meta">
        <span title={page.file_name}>{basename(page.file_name)}</span>
        <span>{formatPage(page.page_no, page.printed_page_no)}</span>
        {page.text_source === 'ocr' && <span className="tag">OCR 识别</span>}
      </div>

      <div className="page-nav">
        <button
          type="button"
          disabled={!page.prev_page_id}
          onClick={() => page.prev_page_id && onNavigate(page.prev_page_id)}
        >
          ← 上一页
        </button>
        <button
          type="button"
          disabled={!page.next_page_id}
          onClick={() => page.next_page_id && onNavigate(page.next_page_id)}
        >
          下一页 →
        </button>
      </div>

      {highlight && !parts.matched && (
        <p className="page-hint">
          本页未找到该事实的原文片段{page.next_page_id ? '，试试下一页' : ''}。
        </p>
      )}
      {parts.matched && parts.offset > 200 && (
        // 命中的句子可能在一千多字之后（实测第 63 页的利润表在第 1269 字，
        // 而页首是资产负债表的尾部）。**告诉用户已经定位好了、不用自己找。**
        <p className="page-hint page-hint-ok">
          已定位到原文第 {parts.offset} 字处，已自动滚动过去。
        </p>
      )}

      <pre className="page-text">
        {parts.matched
          ? parts.nodes.map((node, i) =>
              node.hit ? (
                <mark key={i} ref={markRef}>
                  {node.text}
                </mark>
              ) : (
                <span key={i}>{node.text}</span>
              ),
            )
          : page.text ?? '（本页没有可提取的文本）'}
      </pre>
    </section>
  )
}

interface Segment {
  hit: boolean
  text: string
}

/**
 * 把原文切成「命中 / 未命中」若干段。
 *
 * `source_text` 是解析阶段存下来的片段，可能与页面正文有空白差异，
 * 所以先试整段匹配，失败再退一步用去空白后的核心部分匹配。都不中就
 * `matched: false`，由调用方给出提示——**静默不高亮会让人以为事实没有出处**。
 */
function splitByHighlight(
  text: string,
  needle: string | null,
): { nodes: Segment[]; matched: boolean; offset: number } {
  if (!needle || !text) return { nodes: [], matched: false, offset: -1 }

  let index = text.indexOf(needle)
  let length = needle.length

  if (index < 0) {
    // 退一步：拿掉空白再找。原文常有「应付账款  (五)29」这种多空格。
    const squeezed = needle.replace(/\s+/g, '')
    if (squeezed.length >= 4) {
      const flatText = text.replace(/\s+/g, '')
      const flatIndex = flatText.indexOf(squeezed)
      if (flatIndex >= 0) {
        // 把扁平索引映射回原文索引
        let count = 0
        let start = -1
        let end = -1
        for (let i = 0; i < text.length; i += 1) {
          if (!/\s/.test(text[i])) {
            if (count === flatIndex && start < 0) start = i
            if (count === flatIndex + squeezed.length - 1) {
              end = i + 1
              break
            }
            count += 1
          }
        }
        if (start >= 0 && end > start) {
          index = start
          length = end - start
        }
      }
    }
  }

  if (index < 0) return { nodes: [], matched: false, offset: -1 }

  return {
    offset: index,
    nodes: [
      { hit: false, text: text.slice(0, index) },
      { hit: true, text: text.slice(index, index + length) },
      { hit: false, text: text.slice(index + length) },
    ].filter((s) => s.text.length > 0),
    matched: true,
  }
}
