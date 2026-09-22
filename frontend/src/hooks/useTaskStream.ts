/**
 * 订阅一个任务的 SSE 事件流。
 *
 * 这个 Hook 是任务时间线的全部技术难点所在，单独拎出来是为了让另外 7 个页面
 * 直接复用——**不要**在每个页面里各写一遍 EventSource。
 *
 * 三件必须做对的事：
 *
 * 1. **按类型逐个注册监听。** 服务端发的是具名事件（`event: step.started`），
 *    它**不会**触发 `onmessage`。只写 onmessage 的话页面一片安静，
 *    而且不报错。类型清单来自 `types/contract.ts`，由后端导出，不是手抄的。
 * 2. **断线重连不丢帧。** EventSource 重连时会自动带 `Last-Event-ID`，
 *    服务端从该序号之后补发（见 `app/api/events.py`）。
 *    所以这里**不要**自己重连、也不要自己记序号——那会和浏览器的内建行为打架，
 *    结果是把补发的事件处理两遍，时间线上凭空多出两个步骤。
 * 3. **终态就关流。** 服务端在 `task.completed` / `task.failed` 之后会关掉连接，
 *    但浏览器会把它当成断线并**自动重连**，于是完成的任务永远显示「重连中」。
 *    所以收到终态事件后必须显式 `close()`。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { EVENT_TYPES, TASK_ENDING_EVENTS } from '../types/contract'
import type { TaskEvent } from '../types/contract'

/** 时间线上的一行。由事件推导出来，不是后端直接给的。 */
export interface TimelineItem {
  key: string
  kind: 'plan' | 'step' | 'tool' | 'review' | 'note'
  stepId: string | null
  seq: number | null
  text: string
  /** 与后端 StepStatus 同源 */
  status: 'running' | 'succeeded' | 'failed' | 'retrying' | 'skipped' | 'info'
  detail?: string
}

interface Payload {
  seq?: number
  name?: string
  step?: string
  tool?: string
  status?: string
  summary?: string
  error?: string
  message?: string
  prompt?: string
  ref?: string
  duration_ms?: number
  deterministic?: boolean
  /** 步骤的结构化产出。只有 step.succeeded 带它。 */
  value?: unknown
}

/**
 * 一步的结构化产出。
 *
 * `summary` 是给人看的一句话，`value` 才是**能点开的东西**——公式、入参、
 * 逐年数据点、以及每个点的年报出处。
 *
 * 这两者必须分清：只拿 `summary` 去渲染，界面上就只剩一句
 * 「营业收入 2014–2024：187,414 → 322,116 百万元」，用户没有任何办法核对它。
 * 而「点任意结论回到计算过程」正是这个系统区别于「AI 财报摘要工具」的地方。
 */
export interface StepResult {
  seq: number
  stepId: string | null
  name: string
  summary: string
  /** 工具返回的形状：`{ result, formula, inputs }`，或 Skill 自己的对象 */
  value: Record<string, unknown> | null
}

function asPayload(e: TaskEvent): Payload {
  return (e.payload ?? {}) as Payload
}

/** 这条事件是不是「任务到此为止」。清单由后端导出（contract.ts）。 */
function isTaskEnding(type: string): boolean {
  return (TASK_ENDING_EVENTS as readonly string[]).includes(type)
}

/**
 * 把一条事件翻译成时间线上的一行。返回 null 表示这条事件不上时间线
 * （如 heartbeat——它是保活用的，显示出来只会刷屏）。
 */
function toItem(e: TaskEvent): TimelineItem | null {
  const p = asPayload(e)
  // step_id 在契约里是可选的（heartbeat 这类事件没有步骤），这里归一成 null，
  // 免得下游到处写 `?? null`
  const base = { stepId: e.step_id ?? null, seq: e.seq }
  switch (e.type) {
    case 'plan.created':
      return { ...base, key: `p${e.seq}`, kind: 'plan', text: p.message ?? '计划已生成', status: 'info' }
    case 'step.started':
      return { ...base, key: `s${e.seq}`, kind: 'step', text: p.name ?? '步骤', status: 'running' }
    case 'step.succeeded':
      return { ...base, key: `s${e.seq}`, kind: 'step', text: p.name ?? '步骤', status: 'succeeded', detail: p.summary }
    case 'step.failed':
      return { ...base, key: `s${e.seq}`, kind: 'step', text: p.name ?? '步骤', status: 'failed', detail: p.error }
    case 'step.retrying':
      return { ...base, key: `s${e.seq}`, kind: 'step', text: `${p.name ?? '步骤'} 重试中`, status: 'retrying', detail: p.error }
    case 'step.skipped':
      return { ...base, key: `s${e.seq}`, kind: 'step', text: `${p.name ?? '步骤'} 已跳过`, status: 'skipped', detail: p.message }
    case 'tool.called':
      return { ...base, key: `t${e.seq}`, kind: 'tool', text: `调用 ${p.tool ?? '工具'}`, status: 'running' }
    case 'tool.result':
      return {
        ...base, key: `t${e.seq}`, kind: 'tool', text: p.tool ?? '工具',
        status: p.status === 'failed' ? 'failed' : 'succeeded',
        detail: p.summary ?? p.error,
      }
    case 'review.required':
      return { ...base, key: `r${e.seq}`, kind: 'review', text: '需要人工确认', status: 'info', detail: p.prompt }
    case 'warning':
      return { ...base, key: `w${e.seq}`, kind: 'note', text: p.message ?? '警告', status: 'info' }
    default:
      return null
  }
}

export interface TaskStream {
  events: TaskEvent[]
  timeline: TimelineItem[]
  /** 各步骤的结构化产出，按 seq 升序。结果面板渲染的就是它。 */
  results: StepResult[]
  /** 'idle' | 'streaming' | 'done' | 'error' */
  phase: 'idle' | 'streaming' | 'done' | 'error'
  /** 连接状态：断线时页面要能显示「重连中」，而不是假装一切正常 */
  connected: boolean
  error: string | null
  subscribe: (taskId: string) => void
  reset: () => void
}

export function useTaskStream(): TaskStream {
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [timeline, setTimeline] = useState<TimelineItem[]>([])
  const [results, setResults] = useState<StepResult[]>([])
  const [phase, setPhase] = useState<TaskStream['phase']>('idle')
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const sourceRef = useRef<EventSource | null>(null)

  const close = useCallback(() => {
    sourceRef.current?.close()
    sourceRef.current = null
    setConnected(false)
  }, [])

  // 组件卸载时必须关掉连接：不关的话，切页面会留下一堆后台连接，
  // 而且它们还在往已经卸载的组件里 setState。
  useEffect(() => close, [close])

  const reset = useCallback(() => {
    close()
    setEvents([])
    setTimeline([])
    setResults([])
    setPhase('idle')
    setError(null)
  }, [close])

  const subscribe = useCallback(
    (taskId: string) => {
      close()
      setEvents([])
      setTimeline([])
      setResults([])
      setError(null)
      setPhase('streaming')

      const url = `/api/tasks/${encodeURIComponent(taskId)}/events`
      const es = new EventSource(url)
      sourceRef.current = es

      es.onopen = () => setConnected(true)
      es.onerror = () => {
        // 终态关流时也会走到这里（服务端已经关了连接），所以只在任务没结束时
        // 才当成错误。浏览器会自己重连，并把 Last-Event-ID 带上——**别插手**。
        setConnected(false)
        if (sourceRef.current) setError('连接中断，浏览器正在自动重连…')
      }

      const handle = (ev: MessageEvent) => {
        let parsed: TaskEvent
        try {
          parsed = JSON.parse(ev.data) as TaskEvent
        } catch {
          return // 单条坏帧不该让整条时间线停住
        }
        setEvents((prev) => [...prev, parsed])
        const item = toItem(parsed)
        if (item) setTimeline((prev) => [...prev, item])

        if (parsed.type === 'step.succeeded') {
          const p = asPayload(parsed)
          const seq = p.seq
          if (typeof seq === 'number') {
            // 按 seq 覆盖而不是追加。EventSource 重连时服务端会补发历史，
            // 同一步骤会到两次；追加的话结果面板会出现两份同样的表格，
            // 而且数字完全一样，看不出哪个是多余的。
            setResults((prev) => {
              const next = prev.filter((r) => r.seq !== seq)
              next.push({
                seq,
                stepId: parsed.step_id ?? null,
                name: p.name ?? `步骤 ${seq}`,
                summary: p.summary ?? '',
                value: (p.value ?? null) as Record<string, unknown> | null,
              })
              return next.sort((a, b) => a.seq - b.seq)
            })
          }
        }

        // `type` 在契约里是 string（线上就是字符串），所以这里要按值比，
        // 不能指望 TS 帮忙收窄——真正决定「是不是终态」的是后端的白名单。
        if (isTaskEnding(parsed.type)) {
          setPhase(parsed.type === 'task.completed' ? 'done' : 'error')
          if (parsed.type === 'task.failed') {
            setError(asPayload(parsed).error ?? '任务失败')
          }
          // ★ 必须显式关闭：不关的话浏览器会把「服务端正常关流」当成断线，
          //   无限重连，已完成的页面永远显示「重连中」。
          close()
        }
      }

      for (const type of EVENT_TYPES) {
        es.addEventListener(type, handle as EventListener)
      }
    },
    [close],
  )

  return { events, timeline, results, phase, connected, error, subscribe, reset }
}
