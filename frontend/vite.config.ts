import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // 开发时把 /api 与 /health 转给后端，前端一律用**相对路径**请求。
    //
    // 走代理而不是直连 http://127.0.0.1:8000，是为了绕开跨域：直接连的话，
    // 后端只放行 localhost:5173，用 127.0.0.1:5173 打开页面就会被浏览器拦掉，
    // 而报错是一句笼统的 CORS 提示，和真正的原因（换个主机名而已）离得很远。
    //
    // ⚠ SSE 走这个代理没问题，但**不要**给它加 `ws: true`——
    //   任务事件流是普通 HTTP 长连接，不是 WebSocket。
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
