# 07 · 第三方依赖、版本、来源与许可证

> 赛事要求列明第三方组件的**名称、版本、来源、许可证及使用范围**。
> 本文件是这一项的正式回答，随代码提交。
>
> 版本以 `backend/requirements.lock.txt`（`pip freeze` 产物）与
> `frontend/package-lock.json` 为准；下表填的是**当前开发环境实测版本**。

---

## 一、后端（Python）

### 1.1 直接依赖

| 名称 | 版本 | 来源 | 许可证 | 使用范围 |
|---|---|---:|---|---|
| FastAPI | 0.141.1 | pypi.org | MIT | REST 接口框架，`backend/app/api/` |
| Uvicorn | 0.53.0 | pypi.org | BSD-3-Clause | ASGI 服务器，本地运行 |
| python-multipart | 0.0.32 | pypi.org | Apache-2.0 | 年报 PDF 上传（multipart/form-data） |
| Pydantic | 2.13.5 | pypi.org | MIT | 数据契约唯一真源，`backend/app/schemas/` |
| pydantic-settings | 2.15.0 | pypi.org | MIT | 读取 `.env` 配置 |
| pandas | 3.0.6 | pypi.org | BSD-3-Clause | 黄金集评测、表格比对等**离线**分析脚本。**不参与**财务计算链路——计算一律走 `app/engine/` 的 `Decimal` 纯函数 |
| **PyMuPDF** | **1.28.2** | pypi.org | **AGPL-3.0** | PDF 文本层与坐标抽取，`backend/app/parsing/` |
| pdfplumber | 0.11.10 | pypi.org | MIT | 财务报表表格结构抽取 |
| pypdfium2 | 5.13.0 | pypi.org | Apache-2.0 / BSD-3-Clause | pdfplumber 的渲染后端（间接依赖），页面截图供「查看原页」 |
| openai | 3.16.2 | pypi.org | Apache-2.0 | 调用 DeepSeek 的 OpenAI 兼容接口 |
| pytest | 9.1.1 | pypi.org | MIT | 单元测试 |
| httpx | 0.28.1 | pypi.org | BSD-3-Clause | FastAPI TestClient 依赖 |

### 1.2 间接依赖

其余为上述包的传递依赖（starlette、anyio、numpy、pillow、pdfminer.six、
python-dateutil、typing-extensions 等），完整清单见 `requirements.lock.txt`。
它们的许可证均为 MIT / BSD / Apache-2.0 家族，不含传染性条款。

### 1.3 Python 标准库

`sqlite3`、`decimal`、`json`、`hashlib`、`csv`、`zipfile` 等均属 Python 标准库，
随 Python 解释器分发（PSF License），**不计入第三方依赖**。

> 数据库使用标准库 `sqlite3`（SQLite 本身为 Public Domain），
> 因此本项目**不需要安装任何 SQL 软件**，也不引入 ORM。

---

## 二、前端（Node.js）

| 名称 | 版本 | 来源 | 许可证 | 使用范围 |
|---|---:|---|---|---|
| React | 19.2.8 | npmjs.com | MIT | 界面框架 |
| React DOM | 19.2.8 | npmjs.com | MIT | 渲染 |
| @tanstack/react-table | 9.2.4 | npmjs.com | MIT | 财务事实表、主张对照表 |
| react-dropzone | 20.1.2 | npmjs.com | MIT | 年报 PDF 拖拽上传 |
| Vite | 8.3.0 | npmjs.com | MIT | 构建与开发服务器 |
| TypeScript | 6.0.2 | npmjs.com | Apache-2.0 | 类型系统 |
| ESLint | 10.10.0 | npmjs.com | MIT | 代码检查 |

### 计划引入（尚未安装，引入时同步更新本表）

| 名称 | 许可证 | 计划用途 |
|---|---|---|
| Ant Design | MIT | 组件库 |
| ECharts | Apache-2.0 | 趋势折线、同业柱状、敏感性热力图 |

> 前端**不做任何业务计算**。同比、现金转化率、一致性分数与估值全部由后端
> `app/engine/` 计算后经 REST 下发。前端只负责渲染与交互，因此上表中
> 不含任何数值计算库。

---

## 三、模型服务

| 项目 | 说明 |
|---|---|
| 名称 | DeepSeek（`deepseek-chat` 系列） |
| 版本 | 以调用时 API 返回的模型标识为准，逐次记入 `llm_call` 表与 `run_manifest` |
| 调用方式 | HTTPS，OpenAI 兼容接口（`/v1/chat/completions`），经 `openai` SDK |
| 使用范围 | **仅**用于：MD&A 可验证主张的抽取与结构化、主张—指标匹配的兜底判定、报告文字组织 |
| **不用于** | 一切数值计算。同比、勾稽、比率、诊断指数、DCF、敏感性分析全部由 `app/engine/` 的确定性纯函数计算 |
| 密钥 | 仅存于本地 `backend/.env`，不进版本库、不进前端、不进日志 |
| 权重 | 模型权重不随本仓库分发，不涉及权重许可 |

> **边界由代码强制，不只是约定**：`app/agents/guards.py` 的 `assert_numbers_grounded`
> 会检查模型输出文本里的每个数字是否出现在本次工具结果集合中，找不到即判定为编造
> 并触发重写；重写仍失败则降级为模板文本并标记 `needs_review`。

---

## 四、数据来源

| 项目 | 说明 |
|---|---|
| 年报原始 PDF | 上海证券交易所、深圳证券交易所、巨潮资讯网等**公开渠道**下载 |
| 使用范围 | 仅用于竞赛演示与解析验证 |
| 是否随仓库提交 | **否**。原始 PDF 体积较大且非本项目版权，见 `.gitignore` |
| 随仓库提交的内容 | 提取后的结构化产物、黄金标注集、字段字典种子数据 |

---

## 五、⚠ PyMuPDF 的 AGPL-3.0 与本次决策

### 5.1 事实

PyMuPDF 采用 **AGPL-3.0**（GNU Affero General Public License v3.0），是**强传染性**
许可证。AGPL 第 13 条要求：若修改后的程序通过网络向用户提供服务，必须向这些用户
提供对应源代码。这与 MIT / BSD 家族的依赖性质完全不同。

### 5.2 本项目的决策

> **保留 PyMuPDF，接受 AGPL-3.0。**
>
> 决策依据：本项目**仅用于参加 2026 年北京市大学生金融人工智能竞赛**，
> 无任何商业化计划，不对外提供网络服务，不随附任何闭源分发。AGPL 的核心义务
> 在「通过网络提供服务」与「分发」这两种场景下才被触发，本项目两种都不涉及；
> 且本项目本身即为开源参赛作品，源代码全部提交给赛事评审。

**决策人**：项目组　**日期**：2026-09-22

### 5.3 若将来需要规避

如果后续出现商业化、对外提供服务或需要闭源分发的情形，替换路径已经验证可行：

- 文本与坐标抽取：**pdfplumber**（MIT）+ **pypdfium2**（Apache-2.0 / BSD-3-Clause）
- 二者当前**已在本项目的依赖树中**（见 `requirements.lock.txt`），
  替换只需改 `backend/app/parsing/` 的 PDF 后端适配层，不触及上层逻辑

因此**解析层必须保持 PDF 后端可替换**：不要在解析层之外直接 `import fitz`。
把 PyMuPDF 的调用收敛在一个模块内，替换成本就是改那一个文件。

### 5.4 风险与缓解

| 风险 | 缓解 |
|---|---|
| 评审对 AGPL 提出疑问 | 本文件 5.1–5.3 即为答复；`README.md` 有指向 |
| 解析层与 PyMuPDF 耦合 | 后端适配层收敛，见 5.3 |
| 将来商业化 | 按 5.3 替换，依赖项已在树中 |

---

## 六、维护约定

- 引入任何新依赖，**必须同步更新本表**；`requirements.txt` 与
  `requirements.lock.txt` 同时更新
- 直接依赖写进 `requirements.txt` 并注明许可证；间接依赖由 lock 文件覆盖
- 许可证属于 **copyleft（GPL / AGPL / LGPL / MPL）** 的依赖，必须在
  第五节这类专门段落中说明决策与替换路径，不能只在上表里写一行
- 引入前端依赖时同样更新第二节，并把 `package-lock.json` 一并提交
