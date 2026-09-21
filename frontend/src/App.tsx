/**
 * 工作台骨架占位。
 *
 * 原有的「拖入 CSV → DuckDB-WASM 查询 → 裸表格显示」链路已整体移除，原因：
 * 赛事要求「计算可复算、执行过程可追溯」，计算必须发生在服务端确定性引擎里并落
 * tool_call 日志；在浏览器里跑 SQL 既无法审计，还会带来约 73MB 的 wasm 体积。
 *
 * 后续这里将重建为 8 页工作台（项目首页 / 文件与解析 / 财务事实 / 财务分析 /
 * MD&A 一致性 / 估值 / 备忘录 / 任务与日志），数据一律来自后端 REST + SSE。
 */
function App() {
  return (
    <div style={{ padding: 24, fontFamily: 'sans-serif' }}>
      <h1>财报叙事一致性分析与情景估值投研工作台</h1>
      <p style={{ color: '#666' }}>
        前端工作台正在重建中。后端服务见 <code>backend/</code>，设计约定见{' '}
        <code>docs/</code>。
      </p>
    </div>
  );
}

export default App;
