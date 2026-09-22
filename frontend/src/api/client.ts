/**
 * REST 客户端。
 *
 * **前端不做任何业务计算。** 这个文件只负责取数与转发，同比、比率、估值一律
 * 由后端 `app/engine/` 算好返回——赛事要求「计算可复算、过程可追溯」，
 * 而浏览器里算的东西没法审计。
 *
 * 请求一律用**相对路径**：开发时 Vite 把它们代理给后端（见 vite.config.ts），
 * 于是不受跨域与主机名写法的影响。要直连别的地址就设 VITE_API_BASE。
 */

import type {
  EventHistoryResponse,
  ProjectListResponse,
  TaskListResponse,
  TaskResponse,
  ValidationErrorResponse,
} from '../types/contract'

const BASE = import.meta.env.VITE_API_BASE ?? ''

/** 后端返回的错误。带上 `detail`，页面直接显示它——**后端已经把它写成中文了**。 */
export class ApiError extends Error {
  readonly status: number
  /** 逐字段的校验问题。422 时非空。 */
  readonly issues: ValidationErrorResponse['detail']

  constructor(status: number, message: string, issues: ValidationErrorResponse['detail'] = []) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.issues = issues
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })

  if (!res.ok) {
    // 后端已经把所有报错都写成了中文（见 app/api/errors.py），
    // 这里不要自己编一句「请求失败」把它盖掉——那句话没有信息量。
    let detail = `HTTP ${res.status}`
    let issues: ValidationErrorResponse['detail'] = []
    try {
      const body = await res.json()
      if (Array.isArray(body?.detail)) {
        issues = body.detail
        detail = body.detail.map((i: { msg: string }) => i.msg).join('；')
      } else if (typeof body?.detail === 'string') {
        detail = body.detail
      }
    } catch {
      /* 响应体不是 JSON，保留 HTTP 状态码 */
    }
    throw new ApiError(res.status, detail, issues)
  }
  return res.json() as Promise<T>
}

export interface CreateTaskBody {
  input: string
  project_id?: string | null
  skill?: string | null
  /** false = 后台执行，进度走 SSE。这是页面上要用的模式。 */
  sync?: boolean
}

export const api = {
  listProjects: () => request<ProjectListResponse>('/api/projects'),

  createTask: (body: CreateTaskBody) =>
    request<TaskResponse>('/api/tasks', { method: 'POST', body: JSON.stringify(body) }),

  getTask: (taskId: string) => request<TaskResponse>(`/api/tasks/${encodeURIComponent(taskId)}`),

  listTasks: (limit = 20) => request<TaskListResponse>(`/api/tasks?limit=${limit}`),

  /**
   * 已经发出的历史事件。
   *
   * 页面刷新后先拉一次这个再订阅实时流：否则从「打开页面」到「第一条事件到达」
   * 之间是空白的，用户会以为没反应。
   */
  eventHistory: (taskId: string) =>
    request<EventHistoryResponse>(`/api/tasks/${encodeURIComponent(taskId)}/events/history`),
}
