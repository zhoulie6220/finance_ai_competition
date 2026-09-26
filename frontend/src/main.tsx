import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createHashRouter, RouterProvider } from 'react-router-dom'

import './index.css'
import App, { Home } from './App.tsx'
import FactTable from './pages/FactTable.tsx'
import Narrative from './pages/Narrative.tsx'

/**
 * 用 **HashRouter 而不是 BrowserRouter**。
 *
 * 演示时可能用 `vite preview` 或任意静态托管，路径路由需要服务端 rewrite
 * 才能刷新不 404。HashRouter 把路由放在 `#` 后面，刷新、直接粘链接、
 * 静态托管都不会出问题。现场演示时刷新一下冒出一个 404，是完全可以避免的风险。
 */
const router = createHashRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <Home /> },
      { path: 'projects/:projectId/facts', element: <FactTable /> },
      { path: 'projects/:projectId/narrative', element: <Narrative /> },
    ],
  },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
)
