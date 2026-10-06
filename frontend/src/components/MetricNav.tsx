import { useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'

import { useApi } from '../api/client'
import type { FactGrid, FactGridRow } from '../api/types'
import { statementLabel } from '../format'

/**
 * 侧栏里的「财务事实」组。
 *
 * 展开之后两行，**顺序是总表在前、指标在后**：
 *
 *     财务事实                    ▾
 *         总表
 *         选择指标看走势           ⌄
 *
 * 两行**同一套样式**——都是普通的侧栏行，没有白底、没有边框、只占一行。
 * （早先指标那一行做成了白底描边的下拉按钮，在一列浅灰行里很突兀。）
 *
 * ⚠ 指标列表按需取数：**只在展开时才请求 `/fact-grid`**。
 * 一进站就拉的话，打开「叙事一致性」也会顺手把 91 个指标的网格拉一遍，
 * 而那一页一个格子都用不上。
 */
export default function MetricNav({ projectId }: { projectId: string }) {
  // 已经在财务事实下面（总表或某个指标）时**默认展开**——
  // 折起来的话，人看不到自己站在哪儿。
  const { pathname } = useLocation()
  const inFacts = /\/facts(\/|$)/.test(pathname)
  const [open, setOpen] = useState(inFacts)
  const [menuOpen, setMenuOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  // ⚠ **从路径里解析，不用 `useParams`。** 这一层是外壳，挂在父路由上，
  // `useParams` 拿不到子路由的 `:metricKey`——它会永远返回 undefined，
  // 于是下拉里永远显示「选择指标看走势」，而**不报任何错**。
  // （App.tsx 取 projectId 用的是同一招，原因一样。）
  const metricKey = /\/facts\/([\w-]+)/.exec(pathname)?.[1] ?? null

  // 从别的页面点进来时也要展开，不能只靠初始值
  useEffect(() => {
    if (inFacts) setOpen(true)
  }, [inFacts])

  const grid = useApi<FactGrid>(
    open && projectId ? `/projects/${projectId}/fact-grid` : null,
  )

  // 点外面 / Esc 关掉菜单。没有这一步，菜单会一直挡着下面的导航。
  useEffect(() => {
    if (!menuOpen) return
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setMenuOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  const groups = useMemo(() => groupByStatement(grid.data), [grid.data])
  const current = grid.data?.metrics.find((m) => m.metric_key === metricKey) ?? null
  const total = grid.data?.periods.length ?? 0

  const pick = (key: string) => {
    setMenuOpen(false)
    navigate(`/projects/${projectId}/facts/${key}`)
  }

  return (
    <div className="side-group" ref={box}>
      <button
        type="button"
        className={'side-link expand' + (inFacts ? ' active' : '')}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="caret">{open ? '▾' : '▸'}</span>
        财务事实
      </button>

      {open && (
        <div className="side-sub">
          {/* **总表在前。** */}
          <NavLink
            to={`/projects/${projectId}/facts`}
            end
            className={({ isActive }) =>
              isActive ? 'side-link sub active' : 'side-link sub'
            }
          >
            总表
          </NavLink>

          {/* 指标在后。风格与上一行完全一致：普通侧栏行，点开才出现菜单。 */}
          <div className="metric-wrap">
            <button
              type="button"
              className="side-link sub metric-trigger"
              onClick={() => setMenuOpen((v) => !v)}
              aria-expanded={menuOpen}
            >
              <span className="nm">{current?.label_cn ?? '选择指标看走势'}</span>
              <span className="caret">⌄</span>
            </button>

            {menuOpen && (
              <ul className="metric-menu side">
                {grid.loading && <li className="grp">正在加载指标…</li>}
                {groups.map(([statement, list]) => (
                  <li key={statement}>
                    <div className="grp">{statementLabel(statement)}</div>
                    {list.map((m) => (
                      <button
                        key={m.metric_key}
                        type="button"
                        className={
                          'metric-item' +
                          (m.metric_key === metricKey ? ' on' : '') +
                          (m.filled === 0 ? ' empty' : '')
                        }
                        onClick={() => pick(m.metric_key)}
                      >
                        <span className="nm">{m.label_cn}</span>
                        {/* 覆盖数：**后端数好的**，前端不重算。
                            91 个指标里有 47 个整列是空的，不标出来的话
                            点进去是一张空图，看起来像系统坏了 */}
                        <span className="fill mono">
                          {m.filled === 0 ? '未采集' : `${m.filled}/${total}`}
                        </span>
                      </button>
                    ))}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/** 按报表分组。两处共用，避免分组顺序打架。 */
function groupByStatement(
  grid: FactGrid | null,
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
  const order = ['income', 'balance', 'cashflow', 'indicator', 'industry', 'disclosure']
  return [...buckets.entries()].sort(
    (a, b) => order.indexOf(a[0]) - order.indexOf(b[0]),
  )
}
