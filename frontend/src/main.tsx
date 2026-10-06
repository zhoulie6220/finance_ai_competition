import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import {
  createHashRouter,
  Navigate,
  RouterProvider,
} from 'react-router-dom'

import './index.css'
import App from './App.tsx'
import { useApi } from './api/client.ts'
import type { Project } from './api/types.ts'
import FactTable from './pages/FactTable.tsx'
import MetricDetail from './pages/MetricDetail.tsx'
import Narrative from './pages/Narrative.tsx'

/**
 * 用 **HashRouter 而不是 BrowserRouter**。
 *
 * 演示时可能用 `vite preview` 或任意静态托管，路径路由需要服务端 rewrite
 * 才能刷新不 404。HashRouter 把路由放在 `#` 后面，刷新、直接粘链接、
 * 静态托管都不会出问题。现场演示时刷新一下冒出一个 404，是完全可以避免的风险。
 *
 * ⚠ **没有首页了。** 公司改成侧栏的下拉之后，那页「三家公司卡片」就没用了：
 * 演示只看宝钢一家，多一次点击只会让 5 分钟的开场白变长。
 */
const router = createHashRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <IndexRedirect /> },
      { path: 'projects/:projectId/facts', element: <FactTable /> },
      // 单个指标的细节。**指标是在左边选的**，见 components/MetricNav.tsx
      { path: 'projects/:projectId/facts/:metricKey', element: <MetricDetail /> },
      { path: 'projects/:projectId/narrative', element: <Narrative /> },
      { path: '*', element: <IndexRedirect /> },
    ],
  },
])

/**
 * 根路径 → 主公司的财务事实页。
 *
 * **不写死项目 id**：项目 id 是解析脚本生成的，写死一个常量之后，
 * 换机器重跑数据就会跳到不存在的项目上（页面空白，且不报错）。
 * 这里取接口返回的第一家——那就是主公司。
 */
function IndexRedirect() {
  const projects = useApi<Project[]>('/projects')

  if (projects.loading) return <div className="async-state">正在加载…</div>
  if (projects.error) {
    return <div className="async-state async-error">{projects.error}</div>
  }
  const first = projects.data?.[0]
  if (!first) {
    return (
      <div className="async-state async-empty">
<div className="async-detail">还没有可分析的项目。</div>
      </div>
    )
  }
  return <Navigate to={`/projects/${first.project_id}/facts`} replace />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
)
