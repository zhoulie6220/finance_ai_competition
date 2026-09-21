# CLAUDE.md

本文件为 Claude Code 提供本仓库的工作指引。

## 这是什么

「2026 年北京市大学生金融人工智能竞赛」参赛作品：**财报叙事一致性分析与情景估值投研工作台**。

系统分析 A 股**能源钢铁**行业上市公司的年报，把管理层在 MD&A 里的表述拆成「可验证主张」，
与后续财务事实交叉验证，算出「叙事—财务一致性诊断指数」，再按公开规则透明地影响
DCF 估值的情景参数。

**核心原则：数字由程序计算，模型只负责理解和组织文字。** 所有同比、勾稽、估值、评分
都必须可复算，且能点回到「文件 → 页码 → 原文」。

**时间线**：初赛 2026-10-18（只需项目计划书 + 5 分钟视频，**不需要可运行系统**）；
决赛 2026 年 11 月底（可运行原型 + 完整源代码 + 现场答辩）。

**团队**：计算机 3 人（甲-后端与数据 / 乙-引擎与 Agent / 丙-前端），
另设会计金融方向同学任规则 owner，不写生产代码。

## 目录

```
frontend/          React + Vite + Ant Design + ECharts（工作台，8 个页面）
backend/
  app/
    api/           REST 路由（薄层，只做校验与调用）
    schemas/       Pydantic v2 数据契约唯一真源
    db/            schema.sql + session.py + repositories
    engine/        确定性计算引擎（纯函数、零 IO、零 LLM）
    parsing/       PDF 文本/表格解析、报表定位、行名映射
    agents/        编排状态机 + router + planner + guards
      llm/prompts/ Prompt 版本化管理
    tools/         Tool 登记表（一份 JSON Schema，REST 与 MCP 共用）
    skills/        4 个主 Skill + 1 个质控
    mcp/           MCP server + client
    retrieval/     FTS5 检索（含短查询回退）
    observability/ 结构化日志、工具 trace、文件访问审计、run_manifest
    data/seed/     字段字典与规则参数的种子 SQL
  scripts/init_db.py
  tests/
docs/              设计规则手册（编号文档，见下）
```

赛事要求提交源码时须包含「智能体编排框架、Tool、Prompt、Skill、MCP、数据处理、
日志记录」七类模块，上表与之一一对应，`README.md` 里有映射表。

## 当前进度

**目录存在不等于功能存在。** 有七个包目前只有 `__init__.py` 占位，是给骨架预留的位置：

| 模块 | 状态 |
|---|---|
| `db/schema.sql`、`db/session.py` | ✅ 41 张表 + 3 个视图；三条硬规则已落到 CHECK 约束 |
| `schemas/` | ✅ 33 个模型，数据契约的机读真源 |
| `engine/normalization.py` | ✅ 周期正常化（19 个 golden case） |
| `retrieval/fts.py` | ✅ FTS5 + 短查询 LIKE 回退 |
| `data/seed/` | ✅ 89 个字段字典、65 条规则参数 |
| `scripts/` | ✅ init_db / export_schemas / gen_data_contract_doc |
| `parsing/` `agents/` `tools/` `skills/` `mcp/` `observability/` `api/` | ⬜ **空包占位** |
| `app/main.py` | ⬜ 未创建 |
| `frontend/src/` | ⬜ 仅占位 `App.tsx` |

`engine/` 目前只有 `normalization.py`；`ratios` / `checks` / `dcf` / `multiples` /
`sensitivity` 尚未实现。

## 常用命令

后端虚拟环境已建好（`backend/.venv`，依赖已锁定在 `requirements.lock.txt`）。
换机器重建时：

```bash
cd backend
python -m venv .venv && .venv\Scripts\activate     # Windows；macOS 用 source .venv/bin/activate
python -m pip install -r requirements.lock.txt     # 复现优先用 lock，不是 requirements.txt
python scripts/init_db.py --force                  # 重建数据库（含字段字典与规则参数）
```

测试（注意 `python -m pytest` 而非裸 `pytest`，否则 `pythonpath = .` 不生效）：

```bash
cd backend
python -m pytest                                                # 全部
python -m pytest tests/unit/engine                              # 单个目录
python -m pytest tests/unit/engine/test_normalization.py        # 单个文件
python -m pytest -k "coverage"                                  # 按名字匹配
python -m pytest tests/unit/engine/test_normalization.py::test_eight_year_window_normalizes
python -m pytest -v                                             # 逐条列出测试名
```

> `pytest.ini` 里刻意不写 `-q`：pytest 的 `-q` 与 `-v` 是互相抵消的计数，
> 一旦在 `addopts` 里加了 `-q`，命令行传 `-v` 也出不来逐条测试名。

改动数据契约后必跑（否则前端类型与字段字典文档会和后端分叉）：

```bash
python scripts/export_schemas.py          # 重新生成 frontend/src/types/contract.json
python scripts/gen_data_contract_doc.py   # 重新生成 docs/01-data-contract.md 的字段字典表格
python scripts/export_schemas.py --check  # 只校验是否同步（可放进 CI）
python scripts/gen_data_contract_doc.py --check
```

改字段字典走 CSV 往返，**不要手改种子文件里的数据块**：

```bash
python scripts/dict_csv.py --export   # SQL → app/data/metric_dictionary.csv（Excel 可直接打开）
python scripts/dict_csv.py --import   # CSV → SQL，先校验后写入，有问题就整体拒绝
python scripts/dict_csv.py --check    # 校验两者同步 + 字典规则
```

多值列（别名、排除词）在 CSV 里用 `|` 分隔，导入时也接受 `;`。校验规则在
`app/db/dictionary.py::validate()`，由测试、CSV 导入和 `init_db.py` 三处共用——
**加规则只改这一处**。

前端：

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
npm run build
```

前端**只能**通过 REST + SSE 取数，不在浏览器里做任何业务计算。

> ⚠ `uvicorn app.main:app --reload` 是最终形态，但 `app/main.py` **尚未创建**，
> 现在执行会失败。见下方「当前进度」。

## 数据库

用 Python **标准库 `sqlite3`**，单文件 `backend/var/finance.db`。不需要安装任何 SQL
软件，不引 ORM，不引向量库。查看数据：`python -m sqlite3 var/finance.db` 或
VS Code 的 SQLite Viewer 扩展。

### ⚠ 连接必须走 `app.db.session.connect()`

`PRAGMA foreign_keys` 是**每连接**生效的。直接 `sqlite3.connect()` 拿到的连接外键是
**关闭**的，全部外键约束静默失效——插进一个引用不存在 `file_id` 的财务事实也不会报错。
`connect()` 会在每条连接上开启外键并自检，自检失败直接抛异常。

### 三条硬规则已落到 CHECK 约束

不是写在文档里，是数据库层面拒绝：

1. **无来源不得进已验证**——`status='validated'` 要求 `value_millions`、`source_text`、
   `source_page`、`unit`、`period`、`scope` 全部齐备
2. **缺失即无行**——不设默认值，绝不插补 0；「未找到」由 `v_fact_grid` 视图生成
3. **不可比必须写原因**——`comparable=0` 时 `incomparable_reason` 非空

配套视图：**指数与估值的查询只允许读 `v_fact_verified`**（只暴露 `validated` 且可比的
行），让不可信数据在 SQL 层就进不来。

### 字段字典的匹配语义：精确匹配，不是包含匹配

`metric_definition` 是「PDF 行名 → 字段键」映射的唯一依据。**别名与排除词都是与
归一化后的行名精确相等**，不是包含。

> ⚠ 为什么不能用包含匹配：`notes_and_ar`（应收票据及应收账款）的别名**包含**
> `应收账款`，而「应收账款」又是它自己的排除词。包含匹配下它会被自己永远挡住。
> 精确匹配下两者各自命中各自的行。

配套约束在 `app/db/dictionary.py::validate()`，测试、CSV 导入与建库三处共用：同一别名
不得挂在两个字段上（跨行业重名除外，由 `project.industry` 先过滤候选）、排除词不得挡住
自己的别名、数字型与文本型的 `unit_kind` 必须自洽、披露事项必须是文本型。

> 反例测试是刻意的：`tests/unit/db/test_metric_dictionary.py` 里每条规则都有一个
> 「构造坏字典，断言它被抓到」的用例。只断言「真实数据通过」是不够的——
> **规则没生效**和**数据恰好干净**看起来一模一样。

**例句来源标记不是装饰。** `example_source='synthetic_example'` 表示该句仍是带
【数值】的标准句，只供解析器回归测试，**不得在界面或报告里当成年报原文展示**；
摘到真实原文后必须同步改成 `annual_report`。表里没有 `example_source` 的例句一律
视为未标注，不得使用。

### `period_kind` 不含 `'prior'`

取值只有 `current` / `instant` / `opening` / `average`，描述的是**数值本身的性质**，
不是它出现在哪份报告里。FY2023 的数字，无论在 2023 年报的「本期」栏还是 2024 年报的
「上期」栏，都是同一笔事实：`period='2023'`、`kind='current'`。若把「上期」也做成一个
kind，同一笔事实会存成两行，任何聚合都重复计算且不报错。

同一事实的**多个来源**（主表 / 主要指标表 / 附注 / 正文，精度往往不同）记在
`fact_observation`，交叉校验后取多数一致值写入 `financial_fact`。

### ⚠ FTS 的 trigram 分词器有 3 字符下限

短于 3 字符的 MATCH **静默返回 0 条**。中文财务术语里「存货」「商誉」「营业」「费用」
「收入」「成本」这类两字词极多，直接查 `page_fts` 会让它们永远搜不出来。

**检索一律走 `app.retrieval.fts.search_pages()`**，它会在短查询时自动回退到 LIKE。

## 计算引擎

`app/engine/` 是「程序负责计算、LLM 只组织语言」这条边界的物理位置。四条铁律：

1. **金额一律用 `Decimal`**，禁止 float；结果以字符串入库
2. **纯函数**——无 IO、无 `datetime.now()`（时间由参数传入）、无 `random`、无全局状态。
   同输入必同输出，这是「结果可复现」的基础
3. **拒绝优于猜测**——口径/期间不可比时返回 `refused`，既不返回数字也不返回 0
4. **每个返回值自带 `formula` + `inputs`**——这是「点击结论回到计算过程」的唯一路径

### ⚠ 周期股不得强行正常化

能源钢铁是强周期行业，不能用最近一年的 FCFF 做永续增长（等于把周期顶当常态）。
口径见 `normalization_run` 与 `rule_config`：

- 主窗口 8 个完整年度，锚定最新完整年度滚动；可比年度不足 7 个则扩展至 10 年；
  仍不足返回 `NORMALIZATION_INSUFFICIENT_DATA`
- 核心是**收入加权的周期中位 EBIT margin**（不是算术平均）
- 必须通过「高低盈利阶段覆盖校验」，且该校验用**相对中枢的绝对落差**判定——
  用分位数判定会恒为真，拦不住任何东西
- EBIT 三套并存：`reported`（主 DCF 默认）、`cross-check`（差异 >5% 进人工复核）、
  `adjusted`（**需会计逐笔批准后才可切换主 DCF**）
- 失败时 `ebit_margin_mid` 被 CHECK 约束**强制为 NULL**。这是刻意的：如果失败也返回
  一个「带警告的数字」，一定会有调用方直接用上

口径参数都在 `app/data/seed/0002_rule_config.sql`，**改数据不改代码**。

## 编辑约定

- **界面文案与代码注释用中文**
- 改动 `schema.sql` 后必须跑 `python scripts/init_db.py --force` 与 `python -m pytest`
- `tests/unit/db/test_schema_integrity.py` 里每条断言都对应一个曾经真实存在、
  且**不会报错只会静默损坏数据**的缺陷。改动 schema 时不要删这些测试
- 写 SQL 种子时注意：**SQL 不做相邻字符串字面量拼接**（Python 会），
  跨行的 `'abc'` `'def'` 是语法错误

## 第三方依赖与许可证

赛事要求列明第三方名称、版本、来源、许可证及使用范围，见 `docs/07-third-party-licenses.md`。

**注意 PyMuPDF 是 AGPL-3.0（强传染性）**，若担心合规问题可换 pdfplumber（MIT）+
pypdfium2。其余：FastAPI / pandas / Pydantic（MIT 或 BSD）、React / Vite / Ant Design /
ECharts、DeepSeek 模型（须说明名称、版本、调用方式与使用范围）。

## 已知的坑

- 前端原为纯前端 DuckDB-WASM 方案，**已整体移除**：赛事要求「计算可复算、过程可追溯」，
  浏览器里跑 SQL 无法审计，且带来约 73MB wasm 体积
- `frontend/tsconfig.app.json` 开了 `noUnusedLocals` / `noUnusedParameters`
  （未使用变量会让 build 直接失败）与 `erasableSyntaxOnly`（禁 enum / namespace），
  但 **`strict` 全家族没开**——写财务计算代码前建议先补上
- 演示现场可能断网，`OFFLINE_MODE` 走预录 LLM cassette 时**界面必须显著标注
  「离线回放模式」**，不得假装实时
