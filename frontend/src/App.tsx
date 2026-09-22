/**
 * 工作台第一页：任务时间线 + 结果面板。
 *
 * 这是「假数据真链路」里那条**真链路**的界面：输入一句话 → 后端拆成步骤 →
 * 时间线逐条亮起 → 右侧出结构化结果 → 点任意数字回到年报原文。
 * 数据全部来自 REST + SSE，**这个文件里没有一行业务计算**——同比、比率、估值
 * 都由后端 `app/engine/` 算好返回。浏览器里算的东西没法审计，
 * 而「可复算、可追溯」是比赛的硬要求。
 *
 * 这一页是给丙的样板：另外 7 个页面照这个结构铺开即可，
 * SSE 订阅的部分已经收在 `hooks/useTaskStream.ts` 里，不用重写。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import './App.css'
import { ApiError, api } from './api/client'
import { EvidenceDrawer, type EvidenceTarget } from './components/EvidenceDrawer'
import { ResultPanel } from './components/ResultPanel'
import { useTaskStream } from './hooks/useTaskStream'
import type { ProjectView } from './types/contract'

const PRESETS = [
  { label: '看财务事实趋势', text: '看一下这家公司的财务事实趋势' },
  { label: '跑一次系统自检', text: '跑一次系统自检' },
]

/** 演示数据的项目名带这个前缀（`scripts/seed_demo.py` 灌的）。 */
const DEMO_MARK = '演示数据'

function isDemo(p: ProjectView): boolean {
  return p.name.includes(DEMO_MARK)
}

/**
 * 默认选中哪个项目。
 *
 * 规则：**真实年报优先，其次年度多的优先**。
 *
 * 实测库里同时躺着宝钢十年（真实）、两家对比公司各三年（真实）、
 * 和一份演示数据。默认停在演示数据上是最糟的：用户第一眼看到的数字
 * 是脚本合成的，而他以为在看年报。年度多的优先则让默认落在主公司上——
 * 主公司正是解析得最深、覆盖最全的那一个。
 *
 * ⚠ 刻意**不按 project_id 排序**：写成 `p-600019` 这种硬编码，
 * 换一家案例公司时就要连前端一起改，而且改漏了不报错，只是默认选错项目。
 */
function pickDefault(projects: ProjectView[]): string {
  const real = projects.filter((p) => !isDemo(p))
  const pool = real.length ? real : projects
  const best = [...pool].sort(
    (a, b) => (b.fiscal_years?.length ?? 0) - (a.fiscal_years?.length ?? 0),
  )[0]
  return best?.project_id ?? ''
}

function App() {
  const [input, setInput] = useState('看一下这家公司的财务事实趋势')
  const [projects, setProjects] = useState<ProjectView[]>([])
  const [projectId, setProjectId] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [evidence, setEvidence] = useState<EvidenceTarget | null>(null)

  const { timeline, results, phase, connected, error, subscribe, reset } = useTaskStream()

  useEffect(() => {
    api
      .listProjects()
      .then((d) => {
        setProjects(d.projects)
        // 已经选过就别覆盖：用户手动切了项目，重渲染时被改回去会很难理解
        setProjectId((cur) => cur || pickDefault(d.projects))
      })
      .catch(() => setProjects([]))
  }, [])

  const current = useMemo(
    () => projects.find((p) => p.project_id === projectId) ?? null,
    [projects, projectId],
  )
  const currentIsDemo = current ? isDemo(current) : false

  const run = useCallback(
    async (text: string) => {
      const value = text.trim()
      if (!value || busy) return
      setBusy(true)
      setErr(null)
      setEvidence(null)
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
              <optgroup label="真实年报">
                {projects
                  .filter((p) => !isDemo(p))
                  .map((p) => (
                    <option key={p.project_id} value={p.project_id}>
                      {p.name}
                    </option>
                  ))}
              </optgroup>
              {projects.some(isDemo) && (
                <optgroup label="演示数据（合成，非年报原文）">
                  {projects
                    .filter(isDemo)
                    .map((p) => (
                      <option key={p.project_id} value={p.project_id}>
                        {p.name}
                      </option>
                    ))}
                </optgroup>
              )}
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

        {currentIsDemo && (
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

          {results.length > 0 && (
            <p className="meta">
              共 {results.length} 步产出 ·{' '}
              {results.reduce((n, r) => n + (r.value ? 1 : 0), 0)} 步带结构化数据
            </p>
          )}
        </section>

        <div className="results grow">
          <h2 className="results-title">
            结果
            {phase === 'done' && <span className="badge ok">可点开核对</span>}
          </h2>
          <p className="muted small">
            表里的每个数字都可以点开，看到<b>公式、入参和它在年报的第几页</b>。
          </p>
          <ResultPanel results={results} onPick={setEvidence} />
        </div>
      </div>

      <EvidenceDrawer target={evidence} onClose={() => setEvidence(null)} />
    </div>
  )
}

export default App
