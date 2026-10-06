import { useEffect } from 'react'

import type { FactCell } from '../api/types'
import { groupDigits } from '../format'

/**
 * 派生格的「怎么算出来的」面板。
 *
 * **这一屏是派生值的证据链。** 「毛利率 5.45%」在年报上找不到出处——
 * 它的出处是「营业收入」和「营业成本」那两行。所以这里给的是：
 *
 *   ① 算式原文
 *   ② 代入公式的每一个数（中文键名，与算式里的措辞对得上）
 *   ③ 参与计算的每一行，**每一行都能再点回它自己的年报页**
 *
 * ⚠ **不给「这一步算出 5.4544」这种自动推导动画**，也不在浏览器里做任何算术。
 *   算式与代入的数都是后端原样给的字符串；这一层只负责排版。
 *   浏览器里算的东西没法审计，而「计算可复算」是这个项目的硬要求。
 *
 * ⚠ 拒绝时（`derived_refused` 非空）**照样要有这一屏**：那时候它显示的是
 *   「为什么算不出来、缺哪一个字段」。少了这一屏，「缺一个字段」和
 *   「这个指标压根不适用」在网格上都是「—」，分不出来。
 */
export default function DerivedModal({
  metricLabel,
  period,
  cell,
  onClose,
  onOpenFact,
}: {
  metricLabel: string
  period: string
  cell: FactCell
  onClose: () => void
  onOpenFact: (factId: string) => void
}) {
  // Esc 关掉。与证据抽屉、下拉菜单同一套手感。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const inputs = Object.entries(cell.derived_inputs)
  const sources = cell.derived_sources

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal derived-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h3>
            {metricLabel}
            <span className="subtitle">
              {period} 年 · <b>派生值</b>
            </span>
          </h3>
          <button type="button" className="link-btn" onClick={onClose}>
            关闭
          </button>
        </header>

        <div className="modal-body">
          {/* 派生值不是年报原文——这句话必须在最上面，不能只在悬浮提示里 */}
          <p className="derived-note">
            这一格<b>不是年报上的数字</b>，是系统按下式算出来的。
            它的出处是下面参与计算的那几行，每一行都能点回原年报页。
          </p>

          {cell.derived_formula && (
            <section className="derived-block">
              <h4>算式</h4>
              <p className="derived-formula">{cell.derived_formula}</p>
            </section>
          )}

          {inputs.length > 0 && (
            <section className="derived-block">
              <h4>代入公式的数</h4>
              <table className="plain-table derived-inputs">
                <tbody>
                  {inputs.map(([k, v]) => (
                    <tr key={k}>
                      <th>{k}</th>
                      <td className="num">{v === 'None' ? '—' : groupDigits(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {sources.length > 0 && (
            <section className="derived-block">
              <h4>参与计算的记录（每行可点回年报原文）</h4>
              <ul className="derived-sources">
                {sources.map((s) => (
                  <li key={`${s.metric_key}-${s.period}`}>
                    <button
                      type="button"
                      className="cell-button"
                      disabled={!s.fact_id}
                      onClick={() => s.fact_id && onOpenFact(s.fact_id)}
                      title={
                        s.fact_id
                          ? `来源：年报第 ${s.source_page ?? '?'} 页`
                          : '这一行没有可回链的事实（多半是它自己也是派生的）'
                      }
                    >
                      {s.label_cn} · {s.period}
                    </button>
                    <span className="num">{s.value ? groupDigits(s.value) : '—'}</span>
                    {s.source_page != null && (
                      <span className="src-page">第 {s.source_page} 页</span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* 拒绝时把理由摆在**最显眼**的地方，不是藏在小字里 */}
          {cell.derived_refused && (
            <section className="derived-block derived-refused">
              <h4>这一格算不出来</h4>
              <p>{cell.derived_refused}</p>
            </section>
          )}
        </div>
      </div>
    </div>
  )
}
