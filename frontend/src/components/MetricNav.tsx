import { useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'

import { useApi } from '../api/client'
import type { FactGrid, FactGridRow } from '../api/types'
import { statementLabel } from '../format'

/**
 * 侧栏里的指标选择器。
 *
 * **选择在左边完成，中间只负责显示。** 点「财务事实」展开这一组，
 * 左边出现下拉，选完中间就是那个指标的走势。
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
  // 两级折叠：面板里先列报表，点开一个才看到它下面的指标。
  // **默认展开当前指标所在的那一组** —— 全折着的话，打开面板看不到
  // 自己站在哪儿，每换一次指标都要点两下。
  const [openGroup, setOpenGroup] = useState<string | null>(null)
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

  // 点外面 / Esc 关掉下拉。没有这一步，下拉会一直挡着下面的导航。
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

  // 网格到了、当前指标也认出来了，就把那一组展开
  useEffect(() => {
    if (current) setOpenGroup(current.statement)
  }, [current])

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
          {/* 下拉与它的菜单要包在一层定位容器里。菜单直接挂在 .side-sub 下
              的话，它相对的是**视口**（因为一路上去没有 positioned 祖先），
              会跑到页面左上角去——而按钮看起来一切正常。 */}
          <div className="metric-wrap">
          <button
            type="button"
            className="metric-select side"
            onClick={() => setMenuOpen((v) => !v)}
            aria-expanded={menuOpen}
          >
            <span className="nm">
              {current?.label_cn ?? '选择指标看走势'}
            </span>
            <span className="caret">⌄</span>
          </button>

          {menuOpen && (
            <ul className="metric-menu side">
              {grid.loading && <li className="grp-note">正在加载指标…</li>}
              {groups.map(([statement, list]) => {
                const expanded = openGroup === statement
                return (
                  <li key={statement}>
                    {/* 第一级：报表。**纯文字，不带图标** —— 指标是数据不是文档，
                        给每一行配个图标反而更乱。 */}
                    <button
                      type="button"
                      className="metric-group"
                      onClick={() => setOpenGroup(expanded ? null : statement)}
                      aria-expanded={expanded}
                    >
                      <span className="caret">{expanded ? '▾' : '▸'}</span>
                      <span className="nm">{statementLabel(statement)}</span>
                      <span className="cnt mono">{list.length}</span>
                    </button>

                    {/* 第二级：指标。缩进排在报表下面，和参考图里那个
                        「文件夹行 + 缩进子项」是同一个层级关系。 */}
                    {expanded &&
                      list.map((m) => (
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
                )
              })}
            </ul>
          )}
          </div>

          {/* 总表：91 × 10 的全量网格。**也在这边点**，
              和上面那个下拉是一组——「各个细节以及总表都先点左边」。 */}
          <NavLink
            to={`/projects/${projectId}/facts`}
            end
            className={({ isActive }) =>
              isActive ? 'side-link sub active' : 'side-link sub'
            }
          >
            总表（全部 91 个指标）
          </NavLink>
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
