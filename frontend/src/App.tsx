/**
 * 工作台第一页：任务时间线。
 *
 * 这是「假数据真链路」里那条**真链路**的界面：输入一句话 → 后端拆成步骤 →
 * 时间线逐条亮起 → 出结构化结果。数据全部来自 REST + SSE，
 * **这个文件里没有一行业务计算**——同比、比率、估值都由后端 `app/engine/`
 * 算好返回。浏览器里算的东西没法审计，而「可复算、可追溯」是比赛的硬要求。
 *
 * 这一页是给丙的样板：另外 7 个页面照这个结构铺开即可，
 * SSE 订阅的部分已经收在 `hooks/useTaskStream.ts` 里，不用重写。
 */

import { useCallback, useEffect, useState } from 'react'
import './App.css'
import { ApiError, api } from './api/client'
import { useTaskStream } from './hooks/useTaskStream'
import type { ProjectView, TaskResponse } from './types/contract'

/** 演示用的快捷入口。②依赖 seed_demo 灌的演示数据。 */
const PRESETS = [
  { label: '跑一次系统自检', text: '跑一次系统自检' },
  { label: '看财务事实趋势', text: '看一下这家公司的财务事实趋势' },
]

const STATUS_CN: Record<string, string> = {
  pending: '待执行',
  planned: '已规划',
  running: '执行中',
  waiting_confirm: '等待确认',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

function App() {
  const [input, setInput] = useState('看一下这家公司的财务事实趋势')
  const [projects, setProjects] = useState<ProjectView[]>([])
  const [projectId, setProjectId] = useState<string>('')
  const [result, setResult] = useState<TaskResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const { timeline, phase, connected, error, subscribe, reset } = useTaskStream()

  useEffect(() => {
    api
      .listProjects()
      .then((d) => {
        setProjects(d.projects)
        // 只有一个项目时直接选中：这一步没有歧义，让用户再点一次是白费一道手续
        if (d.projects.length === 1) setProjectId(d.projects[0].project_id)
      })
      .catch(() => setProjects([]))
  }, [])

  const run = useCallback(
    async (text: string) => {
      const value = text.trim()
      if (!value || busy) return
      setBusy(true)
      setErr(null)
      setResult(null)
      reset()
      try {
        // sync=false：立刻返回 task_id，进度走 SSE。
        // 用同步模式的话，页面会在这条请求上一直等到任务跑完，
        // 中间那段「时间线逐条亮起」就完全看不到了。
        const created = await api.createTask({
          input: value,
          project_id: projectId || null,
          sync: false,
        })
        subscribe(created.task.task_id)
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : String(e))
      } finally {
        setBusy(false)
      }
    },
    [busy, projectId, reset, subscribe],
  )

  // 任务结束后再拉一次详情：时间线上的摘要是流式的，
  // 而这一步的产出（公式、入参）只在详情接口里。
  useEffect(() => {
    if (phase !== 'done' || !timeline.length) return
    const last = [...timeline].reverse().find((t) => t.stepId)
    if (!last?.stepId) return
    const taskId = last.stepId.split('-s')[0]
    api.getTask(taskId).then(setResult).catch(() => {})
  }, [phase, timeline])

  const running = phase === 'streaming'

  return (
    <div className="app">
      <header className="app-head">
        <h1>财报叙事一致性分析与情景估值投研工作台</h1>
        <p className="sub">
          数字由程序计算，模型只负责理解和组织文字 · 每一条结论都能点回原文
        </p>
      </header>

      <section className="panel">
        <div className="row">
          <input
            className="input"
            value={input}
            placeholder="说一句话，例如：看一下这家公司的财务事实趋势"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && run(input)}
            disabled={running}
          />
          <button className="btn primary" onClick={() => run(input)} disabled={running || !input.trim()}>
            {running ? '执行中…' : '运行'}
          </button>
        </div>

        <div className="row wrap">
          <label className="field">
            <span>项目</span>
            <select
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              disabled={running}
            >
              <option value="">（不指定）</option>
              {projects.map((p) => (
                <option key={p.project_id} value={p.project_id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <span className="presets">
            试试：
            {PRESETS.map((p) => (
              <button
                key={p.label}
                className="chip"
                disabled={running}
                onClick={() => {
                  setInput(p.text)
                  run(p.text)
                }}
              >
                {p.label}
              </button>
            ))}
          </span>
        </div>

        {projects.some((p) => p.name.includes('演示数据')) && (
          <p className="hint demo">
            ⚠ 当前项目是<b>演示数据</b>：数字由 <code>scripts/seed_demo.py</code> 合成，
            不是年报原文。证据面板里的原文同样带「演示数据」标记。
          </p>
        )}
        {err && <p className="hint error">{err}</p>}
      </section>

      <div className="cols">
        <section className="panel grow">
          <h2>
            任务时间线
            {running && <span className="badge live">执行中</span>}
            {phase === 'done' && <span className="badge ok">已完成</span>}
            {phase === 'error' && <span className="badge bad">失败</span>}
            {running && !connected && <span className="badge warn">重连中…</span>}
          </h2>

          {!timeline.length && <p className="empty">还没有任务。上面输入一句话试试。</p>}

          <ol className="timeline">
            {timeline.map((item) => (
              <li key={item.key} className={`tl tl-${item.kind} tl-${item.status}`}>
                <span className="dot" />
                <span className="tl-text">{item.text}</span>
                {item.detail && <span className="tl-detail">{item.detail}</span>}
              </li>
            ))}
          </ol>

          {error && <p className="hint error">{error}</p>}
        </section>

        <section className="panel grow">
          <h2>结果</h2>
          {!result && <p className="empty">任务完成后，每一步的产出显示在这里。</p>}
          {result && (
            <>
              <p className="meta">
                任务 <code>{result.task.task_id}</code> ·{' '}
                {STATUS_CN[result.task.status] ?? result.task.status}
                {result.task.skill_key && <> · Skill <code>{result.task.skill_key}</code></>}
              </p>
              {result.steps.map((s) => (
                <div key={s.step_id} className={`step step-${s.status}`}>
                  <div className="step-head">
                    <span className="step-seq">{s.seq}</span>
                    <span className="step-name">{s.name}</span>
                    {s.tool && <code className="step-tool">{s.tool}</code>}
                  </div>
                  {s.output && <pre className="step-out">{s.output}</pre>}
                  {s.error && <pre className="step-out err">{s.error}</pre>}
                </div>
              ))}
            </>
          )}
        </section>
      </div>
    </div>
  )
}

export default App
