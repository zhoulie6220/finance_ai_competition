import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

import { hideSnapshotBanner, isSnapshotMode, useApi } from './api/client'
import type { Health, Project } from './api/types'
import MetricNav from './components/MetricNav'
import OfflineBadge from './components/OfflineBadge'

/**
 * 工作台外壳。
 *
 * 布局照 Codex 那类客户端：**一个左边栏，内容全在里面**。
 *
 *   ├ 产品名（原来放「ChatGPT Work」的位置）
 *   ├ 公司   —— 下拉，选中即切换
 *   ├ 功能   —— 财务事实 / 叙事一致性
 *   ├ 能看什么 —— 说明文字
 *   └ 底部状态
 *
 * 侧栏放「能看什么」而不是「最近打开」——这个系统一次只看一家公司、
 * 两个页面，没有「最近」可言；而新来的人（评委、队友）最需要的恰恰是
 * **一进去就知道这里有什么**。所以那是一块说明，不是历史列表。
 *
 * 用 **HashRouter**（见 main.tsx）而不是 BrowserRouter：演示时用
 * `vite preview` 或任何静态托管，刷新页面不会 404。现场演示时刷新一下
 * 出来一个 404 页，是很没必要的风险。
 */
export default function App() {
  const health = useApi<Health>('/health')
  const projects = useApi<Project[]>('/projects')

  // 从路径里取项目 id，而不是 useParams：外壳是外层布局，
  // useParams 拿不到子路由的参数。解析路径虽然朴素，但不依赖路由嵌套层级。
  const { pathname } = useLocation()
  const projectId = /\/projects\/([^/]+)/.exec(pathname)?.[1] ?? null

  const current = projects.data?.find((p) => p.project_id === projectId) ?? null

  return (
    <div className="shell">
      {/* 离线横幅横跨整个宽度、压在最上面。docs/00 把「回放时装作实时」
          列为诚信问题，所以它不许被折叠进侧栏或藏起来。 */}
      <OfflineBadge health={health.data} />

      {/* 离线快照横幅。**这是诚信要求，不是装饰**：一个会被人当场浏览的
          页面，数据是打包那天冻住的、不会随库变化——不标出来的话，
          看的人会以为这是实时算的。与 OfflineBadge 的区别：那个说的是
          「LLM 走预录磁带」，这个说的是「整页数据都是一份快照」。

          ⚠ `__SNAPSHOT_HIDE_BANNER__` 是**录视频用的那一份**专用的：
          录制下来的视频本来就不是实时演示，横幅在那种场景里是多余的。
          由 `build_offline_page.py --no-banner` 注入；**给会计浏览的
          那一份不加这个参数**，横幅照旧。 */}
      {isSnapshotMode() && !hideSnapshotBanner() && (
        <div className="snapshot-banner" role="status">
          <strong>离线快照</strong>
          <span>
            这一页的数据是打包时冻结的，<b>不是实时计算</b>。
            页面上每个数字都能点回它的来源，但不会随库变化而更新。
          </span>
        </div>
      )}

      {health.data && !health.data.db_ok && (
        <div className="db-warning" role="alert">
          <strong>数据库是空的</strong>
          <span>{health.data.db_hint}</span>
        </div>
      )}

      <div className="app">
        <aside className="side">
          {/* 原来这里写的是「ChatGPT Work ⌄」。
              ⚠ 两行是**手动断的**，不让它自己折：这个标题 20 个字，
              侧栏一窄就会折在「分析」和「与」之间，读起来是半句话。
              两行也正好对应系统做的一件事的**两半**——
              上面是把说法和事实对起来，下面是据此看估值。 */}
          <div className="side-brand">
            <span className="side-brand-line">财报叙事一致性分析</span>
            <span className="side-brand-line">情景估值投研工作台</span>
          </div>

          <div className="side-label">公司</div>
          <CompanyPicker
            projects={projects.data ?? []}
            current={current}
            loading={projects.loading}
          />

          <div className="side-label">功能</div>
          <nav className="side-nav">
            {projectId && (
              <>
                {/* 财务事实是个可展开的组：里面既有指标下拉、又有总表。
                    「各个细节以及总表都先点左边」——选择在左边完成，
                    中间只负责显示。 */}
                <MetricNav projectId={projectId} />
                <SideLink to={`/projects/${projectId}/narrative`}>
                  叙事一致性
                </SideLink>
              </>
            )}
          </nav>

          <div className="side-label">这个工作台能看什么</div>
          <SideHint />

          <div className="side-foot">
            <span>规则版本 v{health.data?.rule_config_version ?? '—'}</span>
            <span className="mono">
              {health.data?.counts?.financial_fact ?? '—'} 条事实
            </span>
          </div>
        </aside>

        <div className="main">
          <div className="content">
            <Outlet />
          </div>
        </div>
      </div>
    </div>
  )
}

function SideLink({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) => (isActive ? 'side-link active' : 'side-link')}
    >
      {children}
    </NavLink>
  )
}

/**
 * 公司切换：一个普通下拉，**选中即切换**。
 *
 * 三家是三个独立的 `project`，接口一次只返回一家的数据——没有并排接口。
 * 所以这里不做多选：允许多选的话，点了第二家页面上什么也不会变，
 * 那就成了一个假控件。下拉底部写清楚这一点。
 */
function CompanyPicker({
  projects,
  current,
  loading,
}: {
  projects: Project[]
  current: Project | null
  loading: boolean
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()
  const { pathname } = useLocation()

  // 点外面关掉。没有这一步的话下拉会一直挡着内容。
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // 换公司时**保留当前页**（财务事实 / 叙事一致性），只换项目——
  // 否则从叙事页点一下公司就掉回财务页，演示时很突兀。
  const go = (p: Project) => {
    // ⚠ 连 **指标键** 一起带过去：从「营业总收入」的详情页换公司，
    // 应当还停在新公司的营业总收入上，而不是掉回总表。
    // 只保留最后一段路径的话，看单指标时一点公司就掉回总表，很突兀。
    const tail = /\/(facts(?:\/[\w-]+)?|narrative)\/?$/.exec(pathname)?.[1] ?? 'facts'
    setOpen(false)
    navigate(`/projects/${p.project_id}/${tail}`)
  }

  const span = (p: Project) =>
    p.fiscal_years.length
      ? `${p.fiscal_years[0]}–${p.fiscal_years[p.fiscal_years.length - 1]}`
      : ''

  return (
    <div className="company" ref={box}>
      <button
        type="button"
        className="company-select"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="name">
          {current?.company_name ?? (loading ? '加载中…' : '—')}
        </span>
        <span className="span mono">{current ? span(current) : ''}</span>
        <span className="caret">⌄</span>
      </button>

      {open && (
        <ul className="company-menu">
          {projects.map((p) => {
            const on = p.project_id === current?.project_id
            return (
              <li key={p.project_id}>
                <button
                  type="button"
                  className={on ? 'on' : ''}
                  onClick={() => go(p)}
                >
                  <span className="nm">{p.company_name}</span>
                  <span className="span mono">{span(p)}</span>
                </button>
              </li>
            )
          })}
          <li className="menu-note">
            选中即切换。三家是三个独立的数据集，一次只看一家。
          </li>
        </ul>
      )}
    </div>
  )
}

/** 侧栏的说明文字。**不是历史记录**，是一进去就知道这里有什么。 */
function SideHint() {
  return (
    <div className="side-hint">
      <dl>
        <dt>财务事实</dt>
        <dd>
          每个年度的三张主表连同附注抽成一张指标网格。选一个指标看它这些年的
          走势，点任意一格回到年报原文。柱子是<b>比上年</b>，红增绿减。
        </dd>
        <dt>叙事一致性</dt>
        <dd>
          管理层的主张逐条与财务事实对照，四态判定，再算诊断指数——
          条件不够就如实说缺什么，不给一个假分数。
        </dd>
        <dt>勾稽校验</dt>
        <dd>资产 = 负债 + 权益。不平就指出缺的是哪一行，而不是把容差调大。</dd>
      </dl>
    </div>
  )
}
