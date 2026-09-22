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

**目录存在不等于功能存在。** 以文件逐个点过的实际状态为准：

| 模块 | 状态 |
|---|---|
| `db/schema.sql`、`db/session.py` | ✅ 42 张表 + 3 个视图；三条硬规则已落到 CHECK 约束 |
| `app/main.py` + `api/` | ✅ 可启动。`uvicorn app.main:app --reload` 已能跑；SEE 事件流通了 |
| `agents/`（状态机 + 编排器） | ✅ 骨架冻结，见下方「冻结的接缝」 |
| `agents/llm/`、`planner`、`router`、`guards.py` | ⬜ **不存在。全仓零 LLM 代码**——见下 |
| `tools/registry.py` | ✅ Tool 登记表；已登记 **6** 个工具（3 个 `system.*`、3 个 `facts.*`） |
| `skills/` | ✅ 契约 + 2 个真 Skill（系统自检、财务事实）；3 个主流程 Skill 待填 |
| `observability/` | ⬜ 空包占位（结构化日志暂落在 `app_log` 表 + `main.py` 中间件） |
| `schemas/` | ✅ 33 个模型，数据契约的机读真源 |
| `engine/normalization.py` | ✅ 周期正常化（19 个 golden case） |
| `engine/ratios.py` | ✅ 同比与比率，含拒绝路径 |
| `engine/checks|index|dcf|multiples|sensitivity` | ⬜ **全部未实现** |
| `retrieval/fts.py` | ✅ FTS5 + 短查询 LIKE 回退 |
| `data/seed/` | ✅ 91 个字段字典、64 条规则参数、14 条旧键名映射 |
| `scripts/` | ✅ init_db / export_schemas / gen_data_contract_doc / dict_csv / seed_demo / parse_reports |
| `parsing/` | ✅ PDF → 三张合并表 → 指标（**22 份真实年报 → 743 条事实**） |
| `mcp/` | ⬜ **空包占位** |
| `frontend/src/` | 🟡 **1/8 页**：任务时间线 + 结果面板 + 证据抽屉，已接真实数据 |
| `docs/` | ✅ 00 / 01 / 02 / 03 / 07；⬜ **04**（指数规则）/ **05** / **06**（演示脚本） |
| 初赛交付物 | ⬜ **项目计划书与 5 分钟视频均未开始**（截止 2026-10-18） |

### ⚠ 两件必须说破的事

**1. 比赛主题是「金融投研智能体构建」，而仓里没有智能体。**
`agents/` 只有状态机和编排器，Skill 靠关键词路由，**没有一句模型调用**。
主张抽取与叙事—事实匹配是这套系统不可替代的部分；没有它，
演示出来的东西与「AI 财报摘要工具」没有区别。

**2. `docs/04-index-rules.md` 是所有下游的前置依赖，且尚未落笔。**
诊断指数的公式 / 阈值 / 到情景的映射属会计口径，**须 K 先定**。
它不落地，`engine/index.py` 只能凭空猜，猜完必返工。

> 分工、排期与上手提示在 **`TEAM.md`**（给人看）；本文件只写给 AI 的规则。

---

## ★ 冻结的接缝 —— 三个人都从这条线往上长

**2026-09-22 建立的骨架。这些文件已经定稿，请在上面扩展，不要重写。**

重写的代价不是"多写一遍"，而是同一个位置出现两套并存的约定：一边发
`step.finished`、另一边发 `step.succeeded`，前端只能显示一半，而且**两边都不报错**。

### 已经冻结的东西

| 文件 | 冻结了什么 | 谁往上长 |
|---|---|---|
| `app/main.py` | 应用装配（lifespan / CORS / 路由注册 / 访问日志中间件 / 异常处理） | 都不改；加路由去 `api/` |
| `app/agents/state.py` | **任务与步骤的状态机**、SSE 事件类型白名单、可注入时钟 | 乙：加事件类型要同时改这三处 |
| `app/api/events.py` | **SSE 事件总线**：seq 单调、有界历史、`Last-Event-ID` 续传、心跳、终态关流 | 都不改 |
| `app/api/routes_tasks.py` | 任务接口形状 + SSE 端点契约 | 丙照此写前端；加字段要同步 `contract.json` |
| `app/tools/registry.py` | **ToolSpec**：一份 JSON Schema 同时供给 REST 与 MCP；调用留痕 | 乙：新工具用 `@tool` 注册即可 |
| `app/skills/base.py` | **Skill 协议**：`can_handle` / `plan` / `run(step, ctx, prior)` | 乙：4 个主流程 Skill 实现它 |
| `app/agents/orchestrator.py` | 建任务 → 规划 → 逐步执行 → 落库 → 发事件的流程 | 乙：只加 Skill，**不改编排器** |
| `app/db/repositories/task_repo.py` | task / task_step / tool_call / app_log 的读写 | 甲：加仓储方法，别绕过它直接写 SQL |
| `app/db/repositories/project_repo.py` | project / file 的读写（含 JSON 列转换） | 甲：加方法，别在路由里写 SQL |
| `app/schemas/api.py` | **HTTP 响应契约**：每个接口返回什么形状 | 加接口在这里加模型；路由必须声明 `response_model=` |
| `app/api/errors.py` | 422 的中文报错与响应说明 | 加校验规则时在这里加错误码的中文 |
| `app/config.py` | 配置与 `Clock` 注入 | 都不改；加配置项在这里加字段 |

### 各自往哪里长（**不要做的事**也写清楚了）

> 排期、交付物与截止日期见 **`TEAM.md`**。这里只写「代码往哪儿长、哪儿不许碰」。

| | 往这里长 | ⚠ 不要做 |
|---|---|---|
| **甲** | `parsing/` 准确率收口、`engine/checks.py` 勾稽校验、`observability/` 文件访问审计、新仓储方法 | 不要改 `orchestrator.py` / `state.py`；不要绕开 `TaskRepository` 直接写 SQL |
| **乙** | `agents/llm/` + `prompts/`、`guards.py`、主张抽取与匹配 Skill、`engine/index.py` | 不要新造状态机或事件名（用 `EVENT_TYPES` 里已有的）；不要让 Skill 直接 `print`/写库；**不要自己改指数公式**——那是 `docs/04` 定的 |
| **丙** | `frontend/src/` 全部 | 不要在前端做任何业务计算；不要自己发明事件类型 |

**为什么要这样切**：乙拿走的是「叙事」，甲拿走的是「数字」，丙拿走「看得见的」。
两边在 `engine/index.py` 碰头——**乙产观测，甲算分值**，而这条传导
（管理层的说法 → 可验证观测 → 诊断指数 → 估值情景参数）正是项目的核心机制。
切在这个位置，三个人之间只有一份接口要商量。

### 三个契约，改动必须同步的地方

1. **SSE 事件类型**：`state.py::EVENT_TYPES` ←→ `schemas/task.py::TaskEvent.type` 的说明
   ←→ 前端分发逻辑。拼错不会报错，只会让那一帧**静默不显示**。
   测试：`tests/unit/agents/test_orchestrator.py::test_every_published_event_has_a_valid_type`
2. **状态取值**：`schemas/enums.py` 的 `TaskStatus` / `StepStatus` ←→ `schema.sql` 的 CHECK
   ←→ `state.py` 的转移表。测试：`test_every_status_appears_in_the_table`
3. **工具留痕**：`tool_call.status` 的取值受 CHECK 约束
   （`'succeeded'` / `'failed'` / `'timeout'`），**不要自创**

### 加一个 Skill 的最小步骤

```python
# app/skills/your_skill.py
class YourSkill:
    key, name_cn, description_cn = "parsing", "解析年报", "..."
    def can_handle(self, request): ...          # 规则路由，关键词优先
    def plan(self, request) -> list[PlannedStep]: ...
    async def run(self, step, ctx, prior) -> StepOutcome: ...

# app/skills/__init__.py
ALL_SKILLS = (SELFCHECK, YourSkill())
```

**不需要改编排器，也不需要改路由。** 工具用 `@tool(...)` 装饰器注册即可
（见 `app/skills/selfcheck.py` 的三个例子）。

### 当前能跑通的东西（先跑一遍再动手）

```bash
cd backend
python scripts/init_db.py --force      # 建库 + 种子数据
python scripts/seed_demo.py            # 灌演示数据（380 条事实，可选但前端要用）
uvicorn app.main:app --reload
# 另开一个终端：
curl -X POST localhost:8000/api/tasks -H "Content-Type: application/json" \
     -d '{"input":"跑一次系统自检","sync":true}'
```

同时看前端（两个终端）：

```bash
cd frontend && npm run dev     # http://localhost:5173
```

页面上输入「看一下这家公司的财务事实趋势」→ 中间时间线逐条亮起 → 右侧出结果。
**这是验证整条链路最快的方式**，比在 `/docs` 上点接口直观得多。

一句话 → 4 个步骤 → 16 条 SSE 事件 → 3 条 `tool_call` 留痕。
**这是"假数据真链路"里的那条真链路**，后面所有 Skill 都往上套。

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

后端**现在就能起来**（先确保跑过 `python scripts/init_db.py`，否则启动自检会拒绝启动
并告诉你该跑什么）：

```bash
cd backend
.venv/Scripts/python.exe -m uvicorn app.main:app --reload   # http://127.0.0.1:8000/docs
```

启动时会做一次自检：数据库在不在、种子数据全不全、Skill 注册上没有。
**失败就拒绝启动**——不带着一个空库跑起来，然后让用户点下按钮才看到 500，
那时的报错跟真正的原因（库是空的）离得很远。

## 解析层

`app/parsing/` 把年报 PDF 变成结构化的报表行。**它不碰数据库**，
落库在 `scripts/parse_reports.py`——分开是为了解析能被单独测试。

    python scripts/parse_reports.py --source "<年报根目录>" [--force]

### ⚠ 列是**从数据里认出来的**，不是从表头推的

最初按表头那一行的词位置推列边界，在宝钢上工作得很好，换一家就崩。
改用数据自身的性质：**报表里的数值是右对齐的**，同一列数值的右边界
重合得极紧（实测差 0.2pt），不同列差上百 pt。按右边界聚类，列自己就浮出来了。

顺带解决了「空单元格」：某行第一列没印东西时，那个 `-` 靠**坐标**落在第二列，
而不是靠数序号被当成第一列。

### ⚠ 附注号也是右对齐的数字

宝钢 2019 的资产负债表里，`1,2,3,…` 聚成了一个「列」，于是「货币资金」读出来是 `1`。
判据：整列都是**没有千分位、没有小数点的小整数**（`_looks_like_note_column`）。

而且附注号**不都是纯数字**：宝钢 2024 写 `51`，宝钢 2015 写 `(五)52`。
后者不算数值格，会粘进行名变成 `其中：营业收入(五)52`——那一行整年映射不上，
表现是「这两年没有营业收入数据」。见 `NOTE_CELL_RE`。

### ⚠ 负数：只换全角负号，不要 lstrip

`parse_number` 里若写成 `cleaned.lstrip("-－—–")`，ASCII 的 `-70,487,849.26`
会被剥成**正数**——资产减值损失、公允价值变动损失全部翻正，
而且没有任何地方会报错。测试里钉死了这一条。

### ⚠ 证据原文用 `display_text`，解析用 `full_text`

两个属性都在 `LogicalLine` 上，区别只有一处：`display_text` 在单元格之间补空格。

    full_text    其中：营业收入51322,115,845,919.76344,500,428,314.62
    display_text 其中：营业收入  51  322,115,845,919.76  344,500,428,314.62

`source_text` 落库的是 `display_text`，因为它就是证据面板里那句「年报原文」——
数字全对而糊成一串时，评审的第一反应是解析坏了。

**别去改 `full_text` 来达到这个效果**：它参与表名、表头、单位、年份的识别，
动它是在拿解析正确性换排版。补空格不改变任何解析结果（解析用的是坐标）。

> ⚠ `display_text` 必须按 `chars` 的**原顺序**走，不能按 x0 重排。
> 折行段的字符是接在后面的（见 `merge_wrapped`），而折行段的 x 又回到左边——
> 重排会把第二段的行名插进第一段数字的中间，拼出一个年报上根本没有的句子。

### 行名要先归一化再精确匹配

年报行名带层级（`一、营业总收入`、`其中：营业收入`），字典别名是干净的科目名。
不归一化实测命中率只有 17%，归一化后 27%（剩下的本来就不该映射——
`流动资产合计`、现金流量表明细行等）。

**归一化只剥层级标记，不剥科目名里的括号**：`所有者权益(或股东权益)合计`
的括号是名字的一部分，剥掉会和别的科目撞名。

匹配**一律精确**，不做包含——`notes_and_ar` 的别名包含 `应收账款`，
而「应收账款」又是它自己的排除词。

### 同一笔事实出现在多份年报里

2023 年的数字，在 2023 年报的「本期」列和 2024 年报的「上期」列里各出现一次。
裁决规则：**以「本期」所在的那份年报为准**。别的年报里对不上的值记成重述
（`restated=1`）并写下差异，不覆盖原始披露值。

实测 743 条事实里有 207 条存在这种差异——**那是重述，不是解析错误**。
宝钢十年跨了三次准则切换（新金融工具、新收入、新租赁），差异是真实的。

### 已知待办（需要会计同学定口径 / 后续阶段）

- **正负号跨年不一致**：2015 年报的 `资产减值损失` 印成正数（损失额），
  2024 年报印成负数（抵减利润）。要按 `sign_convention` 统一。
- 长行名跨三行时（行距与折行间距几乎相同）会拼不完整，
  受影响的是 `以公允价值计量且其变动计入当期损益的金融资产` 这类科目。
- 其余约 5 个字段（折旧摊销、政府补助等）在附注与「主要会计数据」表里，属阶段 2 范围外。

## 前端

React + Vite，**没有引 UI 库**（计划里是 Ant Design，等链路稳了再上——
换掉的是 JSX，协议层不用动）。

### 前端不做任何业务计算

同比、比率、估值一律由后端 `app/engine/` 算好返回。浏览器里算的东西没法审计，
而「计算可复算、过程可追溯」是比赛的硬要求。前端只做两件事：取数、渲染。

### 类型来自后端，不要手抄

`frontend/src/types/contract.ts` 由 `python scripts/export_schemas.py` 生成，
含 89 个类型定义、枚举字面量联合、以及 **SSE 事件类型清单**。
手抄的会在后端加字段时静默过期——所以字段名拼错时 `npm run build` 会直接失败，
这是刻意的。

> ⚠ `types/view.ts` 是**唯一例外，也是唯一的雷**。它描述的是工具产出
> （`payload.value` 那一坨）的形状，而那不是 HTTP 响应模型，导出器看不见它。
> 后端给 `facts.series` 的 point 加一个字段、这里不加，前端**不报错**，
> 只是那个字段永远显示不出来。**改 `app/skills/*.py` 的 `output_schema` 时，
> 顺手看一眼 `types/view.ts`。**

### ⚠ 界面上渲染的是 `value`，不是 `output`

`step.succeeded` 事件的 payload 里有 `summary` 和 `value` 两个东西：

| 字段 | 是什么 | 拿来做什么 |
|---|---|---|
| `summary` | 一句中文，如「营业收入 2014–2024：187,414 → 322,116 百万元」 | 时间线上那一行 |
| `value` | 结构化数据：`{result, formula, inputs}` | **结果面板渲染的全部内容** |

只用 `summary` 渲染是**看起来最像做完、实际最没用**的做法：那一行字没有公式、
没有入参、没有页码，用户没有任何办法核对它。而「点任意结论回到计算过程」正是
本系统区别于「AI 财报摘要工具」的地方——把它丢在传输路上，等于把卖点丢了。

`hooks/useTaskStream.ts` 按 step seq 收下 `value`（**覆盖**而不是追加：
EventSource 重连会补发历史，追加会让同一张表出现两份，而数字一模一样，
看不出哪份是多余的）。

### 工具产出必须带出处

`facts.series` / `facts.margin` 的每个点都带 `fact_id` / `source_file` /
`source_page` / `source_text`。这不是装饰——序列是页面上最常被点的东西。

两个易错点：

1. **同一 (指标, 期间) 有多条时要显式排序取一条**，不能靠 `GROUP BY period`
   配裸列。裸列的取舍由查询计划决定，SQLite 不保证是哪一条；
   出处一旦被展示出来，「随便挑一条」就从看不见变成了看得见的错。
2. **派生值（毛利）的出处是两行**，不是一行。它的 `sources` 里同时有
   「被减数 · 营业收入」和「减数 · 营业成本」——只留一个的话，
   用户点开看到的是半个算式，`收入 − 成本 = 毛利` 在界面上无法自证。

> ⚠ `contract.json` 曾经带头部 `/* */` 注释，**根本不是合法 JSON**，
> 浏览器 import 不了。而 `--check` 一直是绿的（它比文本）。现在头部是
> `$comment` 字段，`test_exported_files_are_actually_usable_by_the_frontend` 盯着。

### SSE 订阅只有一处实现

`frontend/src/hooks/useTaskStream.ts`。**不要在每个页面里各写一遍 EventSource。**
它处理的三件事都不是可选的：

1. **按类型逐个 `addEventListener`**——服务端发的是具名事件，不会触发 `onmessage`。
   清单来自 `contract.ts`，由后端导出。
2. **不自己重连、不自己记序号**——EventSource 重连时自动带 `Last-Event-ID`，
   服务端从该序号后补发。自己再记一遍会把事件处理两遍，时间线上凭空多两个步骤。
3. **收到终态事件就 `close()`**——不关的话浏览器把「服务端正常关流」当成断线，
   无限重连，已完成的页面永远显示「重连中」。

开发时前端走 **Vite 代理**（`vite.config.ts`）请求 `/api`，因此不受跨域影响；
后端另放行了 `localhost` 与 `127.0.0.1` 两种写法（它们**不是**同一个来源）。

## 演示数据

`python scripts/seed_demo.py` 灌一份宝钢 2015–2024 的十年财务事实（380 条）。
它的作用是**把「界面能不能正确渲染」和「PDF 解析得准不准」解耦**——
解析还没做，前端页面照样能开发和验收。

### ⚠ 这些数字是合成的，不是年报原文

所以它在**三个地方**被标成演示数据，而不是靠使用者记得：

| 位置 | 值 |
|---|---|
| `project.name` | 前缀「【演示数据】」，页面上第一眼就能看到 |
| `financial_fact.extractor` | `'demo:seed_v1'`，可按它整批查出/删掉 |
| `financial_fact.source_text` | 前缀「【演示数据】」，**证据面板里也逃不掉** |

第三条最要紧。本系统的卖点是「点任意结论都能回到年报原文」，
演示数据的 `source_text` 若长得和真原文一样，它在证据面板里就是**一句看起来
很真的假话**——与「标为 `annual_report` 就必须带出处」是同一条规则。

### 数据必须自洽，而且从库里就能勾稽

脚本生成后先断言、落库后再**从库里读回来**断言一遍（`verify_from_db`）。
理由：乙 的勾稽检查跑的是 SQL，不是脚本里的内存变量。内存里对、落库时错位，
是这类脚本最典型的翻车方式。守的恒等式：资产 = 负债 + 权益、现金滚动、
毛利 = 收入 − 成本、营业利润 = 毛利 − 期间费用。

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

**标为 `annual_report` 就必须带出处。** 签字文档 §6 要求真实例句同时具备
`example_file`（PDF 文件名）与 `example_page`（页码），由 `metric_definition` 上的
CHECK 强制。理由与硬规则一相同：例句是别名维护的锚点，也是评委核对字典的入口，
**它就是证据**。只写一句「真实年报原句」而不记是哪份文件的哪一页，
那句原文和一句编造的话在库里长得完全一样。

### 旧键名只活在 `metric_key_migration` 里

会计签字文档 A-1 改了 13 个字段键名，A-3 另加一个跨年度主字段映射。旧键名
（`net_profit`、`fixed_assets`、`trade_receivables` …）**不再是 `metric_definition`
的行**，所以：

- 写进 `financial_fact.metric_key` 会被外键拒绝 —— 这是刻意的
- 表结构里也不许残留旧列名（`normalization_year` 曾经留着 `total_profit` 与
  `financial_expense`，同一个东西两个名字）
- 两者分别由 `test_legacy_keys_cannot_be_written_as_facts` 与
  `test_no_live_column_is_named_after_a_legacy_key` 盯着

> 改名本身**不会报错**。队友按早先的设计文档写出 `net_profit`，只会得到
> 「这个字段没有数据」——和「公司没披露这一项」看起来一模一样。映射表的价值
> 在于让仓储层能报出「已改名为 `net_income`（依据 A-1）」这种指向明确的错。

### `rule_config.tier` 决定改一个参数要谁点头

`hard`（默认，会计口径须签字）/ `soft`（提示语、排序）/ `model`（模型参数，
不进种子文件，逐次记进 `run_manifest`）。**不写 `tier` 一律按 `hard` 处理**——
让参数「不算数」需要有人明确声明，而不是靠忘记写 `tier` 偷偷降级。

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

### 对外可见的东西也必须是中文（不只是注释）

代码注释是中文还不够。**用户在浏览器里看得见的一切**都要是中文：`/docs` 的
schema 标题、响应说明、422 的报错句子、枚举在文档里的名字。默认全是英文的，
而且**不会报错**——只是评审看到的中文界面上突然冒出一句
`String should have at least 1 character`。

四条约定。前三条各有一个测试盯着，第 4 条（引擎 `inputs` 的键名）
目前只有人工检查——它散在每个 `RatioResult` 里，抓不动。

1. **每个 Pydantic 模型写 `ConfigDict(title="中文名")`**。不写就退回类名，
   `/docs` 和 `frontend/src/types/contract.json` 里显示的是 `TaskStepView`。
   测试：`tests/unit/schemas/test_chinese_surface.py`
2. **每个枚举挂 `@cn_enum("中文名")`**。⚠ 别改成「继承一个带 title 的基类」——
   Python 的 Enum 不许被有成员的枚举继承，而 `ClassVar` / `nonmember` 也挡不住
   子类里的赋值，`title_cn` 会变成**一个真实的枚举成员**，
   `list(FileRole)` 里凭空多一项。理由与实测写在 `app/schemas/enums.py` 里。
3. **每个路由写 `response_model=`**。返回 `dict[str, Any]` 时 FastAPI 生成的是
   `{"additionalProp1": {}}` 空壳——它看起来像一份文档，实际什么也没说，
   前端也没法据此生成类型。测试：
   `tests/integration/test_api.py::test_every_endpoint_declares_a_response_model`
4. **引擎返回的 `inputs` 的键名也在这条线上。** 它会一路流到证据面板的
   「代入公式的数」上，所以 `{"numerator": …}` 在评审眼里就是一句英文，
   而**没有任何地方会报错**。键名还要和同一个返回值的 `formula` 对得上号：
   公式写「毛利 / 营业收入」，键就得是「毛利」「营业收入」；
   增长率把年份写进键里（「2023 年（上期）」），否则用户没法把
   `(2024 − 2023) / |2023|` 和两个裸数字对上。

> **枚举取值本身（`'annual_report'` 等）不翻译。** 它们要落库、要进 JSON、
> 要与 `schema.sql` 的 CHECK 逐字对上，是**数据**不是文案。要说明取值含义就写进
> 类 docstring（Pydantic 会把它变成 schema 里的 `description`）。

### JSON 列在仓储层转，不往上传

`plan` / `depends_on` / `args` / `payload` / `fiscal_years` 在库里都是 **TEXT**，
出库时必须在这一层转回对象。漏转的后果一律是**静默失效**，不报错：

| 字段 | 不转的后果 |
|---|---|
| `tool_call.args` | 接口返回 `'{}'` 字符串，`call.args.include_llm` → `undefined`，过滤条件静默失效 |
| `project.fiscal_years` | 返回 `'["2024"]'`，JS 里 `for (const y of years)` 逐个**字符**迭代 |
| `file.is_scanned`（0/1） | 字符串 `'0'` 在 JS 里是真值 → **每份文件都被当成扫描件** |

盯着它们的测试：`test_tool_call_args_is_an_object_not_a_string` /
`test_project_json_columns_come_back_parsed` / `test_file_is_scanned_is_a_boolean`。

- **`scripts/` 下的脚本开头必须调 `_console.setup()`**。Windows 控制台默认 GBK，
  中文提示语和 ✓ / ✗ 会抛 `UnicodeEncodeError`——而且是**在活儿全干完之后**才抛，
  用户看到 traceback 会以为写失败了并重跑一遍。会计同学用的 `dict_csv.py --import`
  正是这条路径
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
