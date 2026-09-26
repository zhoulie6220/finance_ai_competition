import { useMemo } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'

import { useApi } from './api/client'
import type { Health, Project } from './api/types'
import OfflineBadge from './components/OfflineBadge'

/**
 * 工作台外壳。
 *
 * 用 **HashRouter**（见 main.tsx）而不是 BrowserRouter：演示时用
 * `vite preview` 或任何静态托管，刷新页面不会 404，也不用配 rewrite 规则。
 * 现场演示时刷新一下出来一个 404 页，是很没必要的风险。
 */
export default function App() {
  const health = useApi<Health>('/health')

  // 从路径里取项目 id，而不是 useParams：外壳是外层布局，
  // useParams 拿不到子路由的参数。解析路径虽然朴素，但不依赖路由嵌套层级。
  const { pathname } = useLocation()
  const projectId = useMemo(() => {
    const m = /\/projects\/([^/]+)/.exec(pathname)
    return m ? m[1] : null
  }, [pathname])

  return (
    <div className="app">
      <header className="app-head">
        <NavLink to="/" className="brand">
          财报叙事一致性分析与情景估值投研工作台
        </NavLink>
        <nav className="app-nav">
          {projectId && (
            <>
              <NavLink to={`/projects/${projectId}/facts`}>财务事实</NavLink>
              <NavLink to={`/projects/${projectId}/narrative`}>叙事一致性</NavLink>
            </>
          )}
        </nav>
        {health.data && (
          <span className="app-status" title={health.data.llm_description}>
            规则版本 v{health.data.rule_config_version}
          </span>
        )}
      </header>

      <OfflineBadge health={health.data} />

      {health.data && !health.data.db_ok && (
        <div className="db-warning" role="alert">
          <strong>数据库是空的</strong>
          <span>{health.data.db_hint}</span>
        </div>
      )}

      <main className="app-main">
        <Outlet />
      </main>
    </div>
  )
}

/** 首页：项目选择器。演示动线的第一步。 */
export function Home() {
  const projects = useApi<Project[]>('/projects')

  return (
    <div className="page">
      <header className="page-head">
        <h2>选择公司</h2>
        <p className="page-note">
          A 股能源钢铁行业。主公司为宝钢股份，另两家为估值可比公司。
        </p>
      </header>

      {projects.loading && <div className="async-state">正在加载…</div>}
      {projects.error && (
        <div className="async-state async-error">{projects.error}</div>
      )}

      <div className="project-list">
        {projects.data?.map((p) => (
          <NavLink
            key={p.project_id}
            to={`/projects/${p.project_id}/facts`}
            className="project-card"
          >
            <div className="project-name">{p.company_name}</div>
            <div className="project-code">{p.stock_code}</div>
            <div className="project-meta">
              {p.fiscal_years[0]}–{p.fiscal_years[p.fiscal_years.length - 1]} ·{' '}
              {p.fiscal_years.length} 个年度
            </div>
            <div className="project-meta">
              已验证事实 {p.fact_count ?? 0} 条 · MD&amp;A 段落 {p.mdna_count ?? 0} 段
            </div>
          </NavLink>
        ))}
      </div>
    </div>
  )
}
