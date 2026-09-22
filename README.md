# 财报叙事一致性分析与情景估值投研工作台

**2026 年北京市大学生金融人工智能竞赛参赛作品**

用 AI 检验管理层叙事是否被财务事实验证，并将结果传导至估值与投资备忘录。

系统分析 A 股**能源钢铁**行业上市公司的年报，把管理层在 MD&A 中的表述拆解为具有明确
主体、对象、期间和方向的可验证主张，与后续财务事实交叉验证，形成「叙事—财务一致性
诊断指数」，再按公开规则透明地影响 DCF 估值的情景权重与参数范围。

> 系统不替代投资决策，也不预测股价。它输出的是带有证据、假设、风险、反向验证条件和
> 跟踪指标的研究结论。

---

## 快速开始

### 环境要求

- **Python 3.12+**（用到 `python -m sqlite3` 命令行；开发环境为 3.13）
- **Node.js 20+**
- 数据库用 Python 标准库 `sqlite3`，**不需要安装任何 SQL 软件**，不需要 Docker

### 后端

```bash
cd backend

python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

python -m pip install -r requirements.txt
python scripts/init_db.py         # 建库并装载字段字典与规则参数
python -m pytest                  # 运行测试
uvicorn app.main:app --reload     # 启动服务（默认 127.0.0.1:8000）
```

> **开发中**：后端**已经能启动**，编排链路已经跑通（一句话 → 任务时间线 →
> 结构化结果 → 工具调用留痕）。已完成：数据库结构、数据契约、周期正常化引擎、
> 检索层、字段字典、REST + SSE、编排状态机、Tool 登记表。
> **尚未实现**：PDF 解析（`app/parsing/`）、4 个主流程 Skill、估值与诊断引擎、
> 以及整个前端。详见 `CLAUDE.md` 的「当前进度」与「冻结的接缝」。

### 跑一次看看

```bash
cd backend
python scripts/init_db.py --force
uvicorn app.main:app --reload          # http://127.0.0.1:8000/docs
```

另开一个终端，让系统跑一次自检：

```bash
curl -X POST http://127.0.0.1:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"input":"跑一次系统自检","sync":true}'
```

一句话会拆成 4 个步骤，产生 16 条 SSE 事件，并留下 3 条 `tool_call` 记录。
这是「假数据真链路」里的那条**真链路**——后面所有 Skill 都往上套。

模型接口的配置见 `backend/.env.example`，复制为 `.env` 后填入 DeepSeek 的 API Key。
未配置密钥时系统仍可启动，但涉及模型调用的功能不可用。

### 前端

```bash
cd frontend
npm install
npm run dev                       # http://localhost:5173
```

### 依赖锁定

`backend/requirements.txt` 声明直接依赖与最低版本；`backend/requirements.lock.txt` 是
`pip freeze` 出的全量锁定版本，评审复现时请优先使用后者：

```bash
python -m pip install -r requirements.lock.txt
```

---

## 比赛七项技术要求 → 目录对应

赛事要求提交的源代码须包含以下核心模块。本仓库的对应关系如下：

| 技术要求 | 目录 | 说明 |
|---|---|---|
| 智能体编排框架 | `backend/app/agents/` | 编排状态机、意图路由、任务规划、执行调度、数字守卫 |
| Tool | `backend/app/tools/` + `backend/app/engine/api.py` | 工具登记表（一份 JSON Schema 同时供给 REST 与 MCP） |
| Prompt | `backend/app/agents/llm/prompts/` | 版本化管理，含 registry 与 CHANGELOG |
| Skill | `backend/app/skills/` | 4 个主流程 Skill + 1 个质量控制 Skill |
| MCP | `backend/app/mcp/` | server（把本系统能力暴露为 MCP 工具）+ client（受限只读文件访问） |
| 数据处理 | `backend/app/parsing/`、`engine/`、`db/`、`retrieval/` | PDF 解析、确定性计算、持久化、检索 |
| 日志记录 | `backend/app/observability/` | 结构化日志、工具 trace、文件访问审计、可复现凭据 |

---

## 目录结构

```
├── docs/                    设计规则手册
│   ├── 00-scope.md              ✅ 系统能做什么、不做什么（适用范围与边界）
│   ├── 01-data-contract.md      ✅ ★数据契约与字段字典
│   ├── 02-accounting-rules.md   ✅ ★会计硬规则与校验清单（会计同学签字）
│   ├── 03-valuation-rules.md    ✅ ★估值规则与周期正常化口径
│   ├── 04-index-rules.md        ⬜ 诊断指数公式、阈值与传导映射
│   ├── 05-assumptions-and-risks.md  ⬜ 主要假设、适用范围与风险因素
│   ├── 06-demo-script.md        ⬜ 现场演示脚本与断网兜底流程
│   └── 07-third-party-licenses.md   ✅ ★第三方名称/版本/来源/许可证/使用范围
│
├── backend/
│   ├── app/
│   │   ├── api/               REST 路由（薄层）
│   │   ├── schemas/           ★数据契约唯一真源（Pydantic v2）
│   │   ├── db/                schema.sql + session.py + repositories
│   │   ├── engine/            ★确定性计算引擎（纯函数、零 IO、零 LLM）
│   │   ├── parsing/           PDF 文本与表格解析、报表定位、行名映射
│   │   ├── retrieval/         FTS5 检索（含短查询回退）
│   │   ├── agents/            编排框架
│   │   ├── tools/  skills/  mcp/
│   │   ├── observability/     日志、审计、可复现凭据
│   │   └── data/seed/         字段字典与规则参数种子
│   ├── scripts/               init_db / export_schemas / gen_data_contract_doc
│   └── tests/
│
└── frontend/                 React + Vite + Ant Design + ECharts
    └── src/types/contract.json   ← 由后端契约自动生成，请勿手改
```

---

## 关键设计约定

这些东西如果不先说清楚，后面一定会返工。

### 三条硬规则已落到数据库层面

不是写在文档里，是 `CHECK` 约束。写不进去的就是真的写不进去：

1. **无来源不得进已验证**——`status='validated'` 要求数值、原文、页码、单位、期间、
   口径全部齐备
2. **缺失即无行**——绝不插补 0；「未找到」由 `v_fact_grid` 视图生成
3. **不可比必须写原因**——并购、重述、季节性、会计政策变更等一律标记并说明

所有进入指数与估值的查询只读 `v_fact_verified` 视图，不可信数据在 SQL 层就进不来。

### 数字由程序计算，模型只负责理解和组织

`backend/app/engine/` 是这条边界的物理位置。引擎是纯函数、用 `Decimal`、零 IO、零
LLM，每个返回值自带 `formula` 与 `inputs`，这是「点击结论回到计算过程」的唯一路径。

### 周期股不得强行正常化

能源钢铁是强周期行业，不能用最近一年的 FCFF 直接做永续增长——那等于把周期顶当成
常态。DCF 的起点必须是穿越一个完整周期的中枢盈利能力：

- 主窗口 8 个完整年度，锚定最新完整年度滚动；可比年度不足 7 个则扩展至 10 年；
  仍不足则返回 `NORMALIZATION_INSUFFICIENT_DATA`，**拒绝出分**
- 核心是**收入加权的周期中位 EBIT margin**
- 必须通过高低盈利阶段覆盖校验
- 失败时中枢值被 `CHECK` 约束强制为 `NULL`——连一个「带警告的数字」都不给

详见 `docs/03-valuation-rules.md`。

### 事实、推论、观点三层标签

系统所有重要结论都拆为三层，页面与导出报告均明确标注：原始披露的数字与原文是
**事实**；程序计算出的同比、现金转化率、一致性分数与估值结果是**推论**；投资逻辑与
风险判断是**观点**。这样避免把推测写成财务事实。

### 检索：不要直接查 page_fts

SQLite FTS5 的 `trigram` 分词器**要求查询至少 3 个字符**，短于 3 字的 MATCH 会**静默
返回 0 条**。中文财务术语有大量两个字的核心词（存货、商誉、营业、费用、收入、成本），
直接查 FTS 会让它们永远搜不出来。

**一律走 `app.retrieval.fts.search_pages()`**，它会在短查询时自动回退到 LIKE 扫描。

---

## 测试

```bash
cd backend
python -m pytest                                  # 全部
python -m pytest tests/unit/engine -v             # 计算引擎
python -m pytest tests/unit/db -v                 # 数据库完整性
python -m pytest tests/unit/schemas -v            # 数据契约
```

`tests/unit/db/test_schema_integrity.py` 里每条断言都对应一个曾经真实存在、且**不会
报错只会静默损坏数据**的缺陷（外键失效、全文索引不同步、同一事实重复入库等）。
改动 `schema.sql` 时不要删这些测试。

## 契约一致性检查

改动数据契约后：

```bash
cd backend
python scripts/export_schemas.py                  # 重新生成前端类型
python scripts/gen_data_contract_doc.py           # 重新生成字段字典文档
python scripts/export_schemas.py --check          # 校验前端类型是否同步
python scripts/gen_data_contract_doc.py --check   # 校验文档是否同步
python scripts/dict_csv.py --check                # 校验字段字典 CSV 与 SQL 是否同步
```

## 维护字段字典

字段字典由会计/金融方向同学负责，但他们不该去手改 SQL 里嵌的 JSON 数组——
`'["营业收入","主营业务收入"]'` 改错一个引号**不会报错**，只会让那个字段静默失效。
所以字典以 CSV 作为人机界面：

```bash
cd backend
python scripts/dict_csv.py --export   # 导出 app/data/metric_dictionary.csv
                                      # Excel 直接打开，多值列用 | 分隔，一格一个
# ……在 Excel 里改……
python scripts/dict_csv.py --import   # 读回来
```

`--import` **先校验、后写入**：候选内容先在内存库里加载并跑完全部规则，任何一条不通过
就原样保留旧文件，并指出是 Excel 的第几行。`init_db.py` 建库时也会跑同一套校验，
坏字典进不了数据库；且重建失败时**旧数据库原封不动**。

校验规则只在 `app/db/dictionary.py::validate()` 里写一次，由测试、CSV 导入、
建库三处共用——加规则只改这一处，不会出现「测试里有、导入脚本里没有」的漏检。

---

## 第三方依赖

赛事要求列明第三方名称、版本、来源、许可证及具体使用范围，见
`docs/07-third-party-licenses.md`。

需要注意 **PyMuPDF 采用 AGPL-3.0**（强传染性许可证）。
**本次参赛决定保留它**：本项目仅用于参赛，无商业化计划、不对外提供服务、
全部源代码提交评审，AGPL 的两类触发场景（通过网络提供服务、闭源分发）都不涉及。
决策依据、风险与替换路径见 `docs/07-third-party-licenses.md` 第五节。
若将来需要规避，替换为 pdfplumber（MIT）+ pypdfium2 即可——两者已在依赖树中，
且解析层刻意保持 PDF 后端可替换。

模型侧使用 DeepSeek（OpenAI 兼容接口），其名称、版本、调用方式与使用范围另在
项目说明中列明；模型权重与商业软件源代码不随本仓库提交。

## 数据来源

样例年报取自交易所与巨潮资讯等公开渠道，仅用于竞赛演示与验证。样例原始 PDF 因体积
原因不入库（见 `.gitignore`），提取后的结构化产物与黄金标注集随代码提交。
