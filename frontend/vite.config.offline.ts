import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * 离线单文件版的构建配置。
 *
 * 与 `vite.config.ts` 只差一处：**把产物编成 IIFE 而不是 ES module**。
 *
 * ## 为什么非改这个不可
 *
 * 发给会计同学的是**一个双击打开的单文件 HTML**（`file://`）。
 * ES module 的脚本在 `file://` 下会被浏览器按 CORS 拦下来，
 * 而**内联**的模块脚本虽然通常能跑，却带着两个隐雷：
 *
 *   · 产物里如果有 `import.meta`（React 生态里很常见，
 *     实测 bundle 里有 `import.meta.resolve`），
 *     那么这段代码**在普通脚本里是语法错误**，连退路都没有；
 *   · 动态 `import()` 一旦被触发，就是一次对 `file://` 的取数，必失败。
 *
 * IIFE 产物没有 `import` / `export` / `import.meta`，
 * 可以用最普通的 `<script>` 加载——那是浏览器**任何协议下都认**的形式。
 *
 * ## 代价
 *
 * IIFE 不支持代码分割，所有东西打进一个文件。这一版正好就是要单文件，
 * 所以代价为零。**这个配置只服务于离线包**，开发与常规构建仍走
 * `vite.config.ts`。
 */
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        format: 'iife',
        // 单入口 + 不分包。少了它 rollup 会因为「iife 不支持多 chunk」报错。
        inlineDynamicImports: true,
        entryFileNames: 'assets/offline.js',
        assetFileNames: 'assets/offline.[ext]',
      },
    },
  },
})
