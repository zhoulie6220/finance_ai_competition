/**
 * 取数工具。**前端所有数据都从这里来**——不在浏览器里做任何业务计算。
 *
 * 赛事硬要求「计算可复算、过程可追溯」，浏览器里跑的算术无法审计，
 * 而且动态语言的浮点结果没法复现。所以这里只负责把后端的字符串搬过来，
 * 展示层只做格式化（`format.ts`），不做算术。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

const BASE = '/api'

export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** 拼查询串，跳过 null / undefined / 空串。 */
function withQuery(path: string, params?: Record<string, unknown>): string {
  if (!params) return path
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue
    search.set(key, String(value))
  }
  const qs = search.toString()
  return qs ? `${path}?${qs}` : path
}

export async function apiGet<T>(
  path: string,
  params?: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(BASE + withQuery(path, params), {
    headers: { Accept: 'application/json' },
    signal,
  })

  if (!res.ok) {
    // 后端写的中文 detail 是给人看的（「这笔事实定位不到原文页，多半是
    // source_page 与 page_no 对不上」）。原样抛出去展示，不要再包一层
    // 「请求失败」把它盖掉——那等于把唯一有用的信息扔了。
    let detail = `请求失败（HTTP ${res.status}）`
    try {
      const body = await res.json()
      if (body && typeof body.detail === 'string') detail = body.detail
    } catch {
      // 响应体不是 JSON（比如网关的 HTML 错误页），用兜底文案
    }
    throw new ApiError(res.status, detail)
  }

  return (await res.json()) as T
}

// ---------------------------------------------------------------- hooks

export interface ApiState<T> {
  data: T | null
  loading: boolean
  error: string | null
  reload: () => void
}

/**
 * 取数 hook。
 *
 * ⚠ **带请求序号守卫**：先发的请求可能后到（网络抖动、后端某条 SQL 慢），
 * 如果不加守卫，过期响应会覆盖掉更新的那份——屏幕上显示的是错数据，
 * 而且**不报任何错**。这是很难查的一类 bug，因为它在本地几乎复现不出来。
 */
export function useApi<T>(
  path: string | null,
  params?: Record<string, unknown>,
): ApiState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  const seq = useRef(0)

  const key = withQuery(path ?? '', params)

  useEffect(() => {
    if (!path) {
      setData(null)
      setLoading(false)
      setError(null)
      return
    }

    const ticket = ++seq.current
    const controller = new AbortController()
    setLoading(true)
    setError(null)

    apiGet<T>(path, params, controller.signal)
      .then((body) => {
        if (ticket !== seq.current) return // 过期响应，丢掉
        setData(body)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        if (ticket !== seq.current) return
        setError(err instanceof Error ? err.message : String(err))
        setData(null)
        setLoading(false)
      })

    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  return { data, loading, error, reload }
}

/**
 * SSE 订阅。
 *
 * ⚠ **StrictMode 在开发模式下会双挂载 effect**，于是一条连接会开两次、
 * 事件也会来两遍。用 ref 守卫：只保留最后一次建立的连接，并在清理时关掉。
 * 不处理的话，开发时时间线会出现重复步骤，而生产环境又不会——很难查。
 */
export function useSse(
  path: string | null,
  onEvent: (event: string, data: unknown) => void,
): { connected: boolean; error: string | null } {
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const handler = useRef(onEvent)
  handler.current = onEvent

  useEffect(() => {
    if (!path) return
    const source = new EventSource(BASE + path)
    let closed = false

    source.onopen = () => {
      if (!closed) setConnected(true)
    }
    source.onerror = () => {
      if (!closed) {
        setConnected(false)
        // EventSource 会自动重连。这里只是把状态如实报出来，
        // 不主动 close——现场演示时断一下能自愈比直接放弃好。
        setError('事件流断开，正在重连…')
      }
    }

    const forward = (name: string) => (ev: MessageEvent) => {
      if (closed) return
      setError(null)
      try {
        handler.current(name, JSON.parse(ev.data))
      } catch {
        handler.current(name, ev.data)
      }
    }
    const onStep = forward('step')
    const onDone = forward('done')
    source.addEventListener('step', onStep as EventListener)
    source.addEventListener('done', onDone as EventListener)

    return () => {
      closed = true
      source.removeEventListener('step', onStep as EventListener)
      source.removeEventListener('done', onDone as EventListener)
      source.close()
      setConnected(false)
    }
  }, [path])

  return { connected, error }
}
