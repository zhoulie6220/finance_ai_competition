# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目状态

这是一个**未完工的脚手架**，目标写着「财报分析 MVP」，但目前唯一跑通的链路是：拖入 CSV → DuckDB 查询 → 裸表格显示。**没有任何财务领域逻辑**——没有报表科目识别、没有比率计算、没有图表。

`README.md` 是未经修改的 Vite 模板原文，不含任何项目信息，不必去那里找背景。

## 常用命令

```bash
npm run dev      # Vite 开发服务器（HMR）
npm run build    # tsc -b && vite build
npm run lint     # eslint .
npm run preview  # 预览 dist 产物
```

**没有测试框架**：无 vitest/jest，无 test script，`src/` 下无任何测试文件。要加测试需先引入整套测试栈。

## 架构

整个应用是**纯前端**的：没有后端、没有服务端数据库、没有持久化，文件不离开浏览器。

### 数据层 `src/db.ts`

唯一的数据库代码，封装 DuckDB-WASM（编译成 wasm 的列存 OLAP 引擎，跑在 Web Worker 里）：

- **`?url` 后缀是强制的**。wasm 和 worker 必须经 `@duckdb/duckdb-wasm/dist/...?url` 形式导入，Vite 才能正确产出资源路径；改成普通 import 会构建失败。
- `MANUAL_BUNDLES` 声明 `mvp` / `eh` 两套包，`selectBundle()` 在运行时按浏览器能力（异常处理支持）二选一。注意**两套都会被打进产物**。
- `dbInstance` 是模块级单例，这是必需的：`main.tsx` 挂了 `<StrictMode>`，`useEffect` 会执行两次，没有这个 guard 会实例化出两个 DuckDB 实例。
- `queryFile()` 的三步流程：`registerFileBuffer()` 把文件写进 DuckDB 虚拟文件系统 → 以**文件名字符串**为表名执行 SQL → Arrow Table 经 `toArray().map(r => r.toJSON())` 转成普通对象数组。

### UI `src/App.tsx`

唯一的组件，所有逻辑集中在这一个文件，样式全部是内联 `style` 对象。**没有 import `App.css`**（那是 Vite 模板残留）。表格是手写裸 `<table>`，并未使用已安装的 `@tanstack/react-table`。

## 已知限制与陷阱

动手前需要知道这些，它们直接影响改造方向：

- **SQL 没有输入口**。`db.ts:39` 里 SQL 是硬编码的 `SELECT * FROM '${file.name}' LIMIT 100`，用户无法提出任何自己的查询。
- **表名靠字符串插值拼接**。文件名含单引号会直接导致 SQL 报错，同时也是注入面。
- **`registerFileBuffer` 从不配对 `dropFile`**。反复拖入同一文件会在虚拟文件系统里累积泄漏。
- **只取 `acceptedFiles[0]`**（`App.tsx:25`），多文件拖入会被静默丢弃。
- **只接受 CSV**（dropzone 限定 `text/csv`）。DuckDB 读不了 `.xlsx`，而真实财报多是 Excel，需要先接一层 SheetJS 之类的转换。
- **无持久化**，刷新页面数据即丢失。
- **没有 `db.terminate()` 清理**。

## 依赖现状

`package.json` 里有两个装了但代码中**完全没用到**的依赖：`@tanstack/react-table`（表格交互引擎）和 `apache-arrow`（仅作为 duckdb-wasm 的传递依赖被间接使用）。`src/assets/` 下的图片和 `src/App.css` 同样是 Vite 模板残留。

## 构建产物

DuckDB 的 wasm 主导了产物体积：`duckdb-eh.wasm` 约 36MB、`duckdb-mvp.wasm` 约 41MB（gzip 后分别约 8.2MB / 9.4MB），且两套都会进产物。应用自身代码仅约 433KB。任何涉及部署或体积优化的工作，这里是唯一的重点。

## TypeScript 配置要点

`tsconfig.app.json` 开了 `noUnusedLocals` / `noUnusedParameters`，**未使用的变量或参数会让 `npm run build` 直接失败**，而不只是警告。另外开了 `erasableSyntaxOnly`，禁止 enum、namespace、构造函数参数属性等不可擦除语法。

## 约定

界面文案与代码注释使用中文。
