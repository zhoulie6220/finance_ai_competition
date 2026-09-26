import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 开发与预览都把 /api 代理到后端，这样开发期完全不用碰 CORS。
// 后端那边仍然保留 CORSMiddleware——不走代理直连时用得上。
const proxy = {
  '/api': {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
  },
}

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
  preview: { port: 5173, proxy },
})
