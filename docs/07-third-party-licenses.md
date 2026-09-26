# 第三方依赖清单

> 赛事要求：使用第三方开源项目、模型、数据或代码的，应在项目说明中列明
> **名称、版本、来源、许可证及具体使用范围**，不得将第三方成果冒充为自主开发。
>
> **版本以 `backend/requirements.lock.txt` 与 `frontend/package.json` 为准**，
> 下表照抄，不是凭印象写的。
>
> ⚠ **提交前请复核一遍**：许可证条款会随版本变化，且本表由开发者整理、
> 非法律意见。合规问题以各项目官方仓库的 LICENSE 文件为准。

---

## 一、后端（Python）

| 名称 | 版本 | 来源 | 许可证 | 使用范围 |
|---|---|---|---|---|
| Python | 3.13.7 | python.org | PSF License | 运行环境 |
| **sqlite3** | 标准库 | Python 标准库 | Public Domain | **唯一的数据库**，单文件 `var/finance.db`；不引 ORM、不引向量库 |
| FastAPI | 0.141.1 | github.com/fastapi/fastapi | MIT | REST 接口框架 |
| Starlette | 1.6.0 | github.com/encode/starlette | BSD-3-Clause | FastAPI 的底层 ASGI 框架（间接依赖） |
| Uvicorn | 0.53.0 | github.com/encode/uvicorn | BSD-3-Clause | ASGI 服务器 |
| Pydantic | 2.13.5 | github.com/pydantic/pydantic | MIT | 数据契约（`app/schemas/`，33 个模型） |
| pydantic-settings | 2.15.0 | github.com/pydantic/pydantic-settings | MIT | 配置读取 |
| pandas | 3.0.6 | github.com/pandas-dev/pandas | BSD-3-Clause | 解析阶段的数据整理 |
| NumPy | 2.5.3 | numpy.org | BSD-3-Clause | pandas 的依赖（间接） |
| httpx | 0.28.1 | github.com/encode/httpx | BSD-3-Clause | FastAPI `TestClient` 依赖，用于接口测试 |
| python-dotenv | 1.2.3 | github.com/theskumar/python-dotenv | BSD-3-Clause | 读 `backend/.env` |
| python-multipart | 0.0.32 | github.com/Kludex/python-multipart | Apache-2.0 | 文件上传（multipart/form-data） |
| **openai** | 3.16.2 | github.com/openai/openai-python | Apache-2.0 | **DeepSeek 走 OpenAI 兼容接口**的客户端 SDK |
| pytest | 9.1.1 | pytest.org | MIT | 自动化测试（182 项） |

### 1.1 ⚠ PDF 解析库的许可证问题

| 名称 | 版本 | 来源 | 许可证 | 使用范围 |
|---|---|---|---|---|
| **PyMuPDF** | 1.28.2 | github.com/pymupdf/PyMuPDF | **AGPL-3.0** ⚠ | PDF 文本与坐标抽取 |
| pdfplumber | 0.11.10 | github.com/jsvine/pdfplumber | MIT | 表格抽取 |
| pdfminer.six | 20260107 | github.com/pdfminer/pdfminer.six | MIT | pdfplumber 的依赖 |
| pypdfium2 | 5.13.0 | github.com/pypdfium2-team/pypdfium2 | Apache-2.0 / BSD-3-Clause | PDF 渲染（可选） |
| Pillow | 12.3.0 | python-pillow.org | MIT-CMU (HPND) | 图像处理 |

> **PyMuPDF 是 AGPL-3.0，强传染性。** 它要求以网络服务形式提供软件时
> 也要向使用者提供完整源码。本项目是竞赛作品、源码需完整提交，
> 这在实践中不构成障碍；但若日后要商业化部署，应换成
> **pdfplumber（MIT）+ pypdfium2**的组合，二者能力可以覆盖本项目所需
> （pdfplumber 表格抽取更好，pypdfium2 负责渲染）。
>
> **决策权在团队**。当前保留 PyMuPDF 是因为 PDF 解析模块尚未入库（见下），
> 实际未调用，替换成本为零——**这是现在换的最佳时机**。

### 1.2 「声明了但当前未使用」的说明

`backend/requirements.txt` 里声明了 `pymupdf`，但**生成数据包的那套解析代码
不在仓库中**（`scripts/parse_reports.py`、`scripts/parse_mdna.py` 从未提交）。
所以当前仓库里没有任何代码 `import fitz`。

这一点必须如实说明：数据包里的 743 条财务事实与 3547 页正文是用那套代码产出的，
而**那套代码的第三方依赖清单与本文档可能不完全一致**。补齐解析模块后需同步更新本表。

---

## 二、前端（TypeScript / JavaScript）

| 名称 | 版本 | 来源 | 许可证 | 使用范围 |
|---|---|---|---|---|
| React | 19.2.8 | react.dev | MIT | UI 框架 |
| React DOM | 19.2.8 | react.dev | MIT | 渲染 |
| React Router | 7.18.4 | reactrouter.com | MIT | 路由（用 HashRouter） |
| Vite | 8.3.0 | vite.dev | MIT | 构建与开发服务器 |
| TypeScript | 6.0.2 | typescriptlang.org | Apache-2.0 | 类型检查 |
| @vitejs/plugin-react | 6.1.1 | github.com/vitejs/vite-plugin-react | MIT | Vite 的 React 支持 |
| ESLint | 10.10.0 | eslint.org | MIT | 代码检查 |

### 2.1 未使用的依赖（如实说明）

`frontend/package.json` 里还留着两个依赖，但**当前没有任何代码引用**：

| 名称 | 版本 | 许可证 | 状态 |
|---|---|---|---|
| @tanstack/react-table | 9.2.4 | MIT | 未使用（原 DuckDB-WASM 链路移除后悬空） |
| react-dropzone | 20.1.2 | MIT | 未使用（可能用于后续「文件与解析页」的文件上传） |

另有 `antd` 与 `echarts` **曾安装但未使用，已移除**——
比赛要求列明第三方的「使用范围」，把没用过的写上去是不准确的。
若后续需要图表（如 A 股红涨绿跌的走势线），再重新引入 `echarts` 并更新本表。

---

## 三、模型

| 名称 | 版本 | 来源 | 调用方式 | 使用范围 |
|---|---|---|---|---|
| **DeepSeek** | `deepseek-chat` | platform.deepseek.com | HTTPS，OpenAI 兼容接口，`openai` SDK | 仅用于**文本理解与组织**：从 MD&A 抽取候选主张、辅助生成解释文字 |

### 3.1 使用边界（重要）

- **模型不参与任何数值计算。** 所有同比、勾稽、估值、评分由
  `backend/app/engine/` 的确定性纯函数完成
- **模型输出必须过数字守卫**（`app/agents/guards.py`）：文本里每个数字都要能在
  本轮工具结果里找到，否则判为编造并拦截
- 每次调用落 `llm_call` 表：模型名、版本、prompt 版本与哈希、参数、原始输出、
  token 数、耗时、是否缓存。**`params` 中不含密钥**
- 演示现场断网时走 `OFFLINE_MODE` 预录回放，**界面显著标注「离线回放模式」**
- 模型权重不提交（赛事允许），本文说明其名称、版本、调用方式与使用范围即可

---

## 四、数据

| 名称 | 来源 | 使用范围 |
|---|---|---|
| 上市公司年度报告 PDF | 巨潮资讯网等公开披露渠道 | 宝钢股份 2015–2024、华菱钢铁 2022–2024、首钢股份 2022–2024，共 16 份 |

- 年报原文页被 `.gitignore` 排除在仓库外（约 94MB），**不随源码提交**
- 仓库内只保留**提取后的结构化数据**（`backend/app/data/samples/`，待补）
  与数据包导入脚本
- 数据包内的 `backend/var/finance.db` 快照同样不入库
- 使用范围限于本竞赛的分析与演示

---

## 五、自主开发范围声明

以下为团队自主实现，未使用第三方同类成果：

- 数据契约（33 个 Pydantic 模型）、数据库 schema（42 表 / 4 视图）
- 字段字典（91 项指标）与规则参数（72 条）的**规则体系与校验逻辑**
- 周期正常化引擎、三表勾稽校验引擎
- 数字守卫、Prompt 版本化注册表、模型客户端的三模式传输
- 前端工作台（表格、证据抽屉、判定着色）

**明确不使用**：ORM、向量数据库、前端 SQL 引擎（原 DuckDB-WASM 方案已整体移除）。

---

> ⚠ **待补**：生成数据包的 PDF 解析模块未入库，其依赖清单需在其提交后补入本表。
> 在此之前，本表反映的是**当前仓库中实际存在并可运行的代码**的依赖。
