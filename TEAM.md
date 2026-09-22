# 分工与协作 —— 三个人怎么干

> 这份文件是给**人**看的。给 **AI** 看的规则在 `CLAUDE.md`——两边内容有重叠，
> 但 CLAUDE.md 才是队友的 AI 会自动读到的那份，所以**约定性的东西一律写进
> CLAUDE.md**，这里只放排期、分工和使用提示。

**今天 2026-09-22。初赛 10-18（还有 26 天），决赛 11 月底。**

---

## 一、先说清楚现在到哪了

**目录存在不等于功能存在。** 下面是逐个文件点过的真实状态：

| 模块 | 状态 | 说明 |
|---|---|---|
| `db/schema.sql` | ✅ | 42 张表 + 3 视图，三条硬规则在 CHECK 里 |
| `api/` + `main.py` | ✅ | 可启动，SEE 事件流通了 |
| `agents/` 状态机 + 编排器 | ✅ 冻结 | 不要重写 |
| `tools/registry.py` | ✅ | 已登记 **6** 个工具 |
| **`parsing/`** | ✅ **最难的已经做完** | 22 份真实年报 → **743 条事实**，每条带文件/页码/原文 |
| `engine/normalization.py` | ✅ | 周期正常化，19 个 golden case |
| `engine/ratios.py` | ✅ | 同比与比率，含拒绝路径 |
| `skills/selfcheck` `skills/facts` | ✅ | 2 个真 Skill |
| `frontend/` | 🟡 **1/8 页** | 任务时间线 + 结果面板 + 证据抽屉，已接真实数据 |
| `docs/00 01 02 03 07` | ✅ | 已写 |

**还是一片空白的：**

| 缺口 | 严重程度 |
|---|---|
| **`agents/llm/`、`guards.py`** | 🔴 **一处 LLM 代码都没有**。比赛主题是「金融投研**智能体**构建」，而现在整套系统是确定性流水线，没有一句模型调用 |
| `engine/index.py`（诊断指数） | 🔴 项目的核心创新，零代码 |
| `engine/checks.py`（勾稽校验） | 🟡 解析质量的下一个台阶 |
| `docs/04-index-rules.md` | 🔴 **所有人都卡在它上面**（见下） |
| 3 个主流程 Skill（主张抽取 / 一致性诊断 / 估值） | 🔴 |
| `engine/dcf | multiples | sensitivity` | 🟡 决赛项 |
| `observability/`、`mcp/` | ⬜ 空包，决赛项 |
| **项目计划书 PDF、5 分钟视频** | 🔴 **初赛交付物，现在零进度** |

### ⚠ 三个必须先说破的事

**1. 初赛不需要可运行系统。** 10-18 只交「项目计划书 PDF + 5 分钟 MP4」。
系统是决赛的交付物。但**视频里必须有东西可演**，所以系统还得到能录像的程度。

**2. 比赛叫「智能体构建」，而我们没有智能体。** 这是目前最大的落差。
主张抽取、叙事与事实的匹配，是这套系统不可替代的部分——没有它，
演示出来的东西和「AI 财报摘要工具」没有区别，而评委正是冲着「智能体」来的。

**3. `docs/04-index-rules.md` 是乙的前置依赖，现在还没写。**
诊断指数的公式、阈值、到情景的映射表，属于会计口径，**必须 K 先定**。
它不落地，乙的 `index.py` 只能凭空猜，猜完还要返工。
**这是当前最该先做的一件事**，比任何代码都靠前。

---

## 二、三人的分工

**代号沿用 `CLAUDE.md` 里的甲 / 乙 / 丙**，各自对号入座。K = 会计金融方向同学，
规则 owner，不写生产代码。

### 甲 · 数据与交付

| 交付物 | 截止 | 说明 |
|---|---|---|
| `engine/checks.py` 勾稽校验 | 10-05 | 用「资产 = 负债 + 权益」「现金滚动」反查解析错误。**不平就优先怀疑解析，不是公司造假** |
| 解析准确率收口 | 10-08 | 长行名折三行、正负号口径两处已知问题 |
| **初赛项目计划书** | **10-12** | 执笔。三人各写自己那块，甲统稿 |
| `observability/` | 决赛 | 文件访问审计 + `run_manifest` |

**边界**：不要改 `orchestrator.py` / `state.py`；加仓储方法，**别绕开
`TaskRepository` 直接写 SQL**。

**为什么是甲**：解析层是甲做的，他最清楚数据从哪来、哪一步可能出错——
而勾稽校验的全部价值就在于「从数字反推解析错在哪」。

### 乙 · 叙事与指数（**关键路径，最重**）

| 交付物 | 截止 | 说明 |
|---|---|---|
| `agents/llm/` 客户端 + `prompts/` | 10-02 | DeepSeek，OpenAI 兼容。Prompt 版本化 |
| 主张抽取 Skill | 10-08 | 从 MD&A 抽「可验证主张」，两段式：先定位候选句，再结构化 |
| 主张—事实匹配 | 10-10 | 四态：支持 / 冲突 / **不可比** / 缺失 |
| `engine/index.py` 诊断指数 | 10-12 | 依 `docs/04-index-rules.md` 实现，**公式由 K 定，乙不许自己改** |
| `agents/guards.py` 数字守卫 | 10-14 | 文本里每个数字必须在本次工具结果里找得到 |

**边界**：不要新造状态机或事件名（用 `state.py::EVENT_TYPES` 里已有的）；
**Skill 里不许 `print`、不许直接写库**。

**为什么是乙**：这是项目的核心创新，也是唯一无法被替代的部分。给乙最多的
时间，别让他被杂事打断。

**⚠ 乙的第一件事不是写代码，是催 K 交 `docs/04-index-rules.md`。**

### 丙 · 界面与演示

| 交付物 | 截止 | 说明 |
|---|---|---|
| 前端：主张一致性页 | 10-10 | 主张表 + 主张—事实对照表（四态配色） |
| 前端：诊断卡 | 10-12 | 构成项 + 触发证据 + 置信度 + `insufficient` 时**不出分** |
| **5 分钟视频脚本** | **10-08** | 执笔。对着比赛须知第 10 节的演示步骤写 |
| **录像与剪辑** | **10-15** | 连续 3 次不挂再录 |

**边界**：**不要在前端做任何业务计算**；不要自己发明事件类型。

**为什么是丙**：视频拍的就是界面上发生的事，最清楚每个画面长什么样的人
来写脚本，返工最少。

### K · 规则 owner（不写生产代码）

- **`docs/04-index-rules.md`** —— 🔴 **最优先**，乙卡在这
- `docs/05-assumptions-and-risks.md` —— 假设、适用范围、风险因素（比赛明确要求）
- 审 `docs/06` 与计划书里的会计口径段落
- **正负号口径**：`资产减值损失` 2015 年报印正数、2024 年报印负数，同一科目
  两种列报。要按 `sign_convention` 统一——**这个只能 K 拍板**

---

## 三、依赖与关键路径

```
K: docs/04 指数规则 ──┐
                      ↓
乙: LLM + 主张抽取 ──→ 主张观测 ──→ 甲/乙: index.py ──→ 丙: 诊断卡
                      ↓                                      ↑
甲: checks.py 勾稽 ───→ 解析准确率 ──→ 事实可信度 ────────────┘
                      ↓
              丙: 视频（把这些串起来演 5 分钟）
```

**关键路径上只有两件事**：`docs/04` 和「LLM + 主张抽取」。
其余都是可以并行或推后的。

### 排期

| 日期 | 里程碑 |
|---|---|
| 9-24 | K 交 `docs/04`；三人各自跑通本地环境 |
| 10-02 | 乙的 LLM 客户端跑通（能发一次请求拿到结构化输出） |
| 10-08 | 主张抽取能出东西；丙的脚本定稿 |
| 10-12 | 计划书初稿 + 诊断指数能出分 |
| 10-15 | 系统能连续录 3 次不挂；视频拍完 |
| **10-18** | **初赛提交** |

### 工期不够时的砍法（按顺序）

1. 先砍**情景估值**（`dcf` / `multiples` / `sensitivity`）——决赛再补
2. 再砍**同业对比页**
3. 最后砍**主张抽取的主题数量**（五类砍到三类，先保证「需求/订单」和「降本增效」）

**绝对不要砍证据链和任务时间线。** 那是比赛的核心考察点，也是这套系统与
普通「AI 财报摘要工具」的唯一区别。**工期不够时砍功能，不砍可追溯性。**

---

## 四、协作约定

### 分支

三个人直接往 `main` 推一定会撞——`CLAUDE.md`、`schemas/`、
`frontend/src/types/contract.ts` 是**共享文件**。约定：

```bash
git checkout -b feat-agent        # 每人一条自己的分支，名字固定
# ……干活……
git add -A && git commit -m "feat: ..."
git pull --rebase origin main     # 先把别人的合进来
git push origin feat-agent        # 推自己的分支
# 在 GitHub 上开 PR 合入 main —— 让另一个人看一眼
```

**共享文件的规矩**：

| 文件 | 规矩 |
|---|---|
| `CLAUDE.md` | 只**追加**自己在的那个小节，不要重排别人的 |
| `backend/app/schemas/` | 改之前先在群里说一声——它是三个人的契约 |
| `frontend/src/types/contract.ts` | **生成文件**，不要手改。改完 schema 的人生成完立刻提交 |
| `docs/` | 各写各的编号文件，不交叉 |

### 提交信息

一行说清楚「做了什么」，中文。别写「update」「fix bug」——那种信息等于没写。

```
feat: PDF 解析层落地，22 份真实年报入库
fix: 负号被 lstrip 剥掉，资产减值损失全部翻正
docs: 补指数规则手册初稿
```

**写「为什么」比写「做了什么」值钱**，尤其是修 bug 时：

```
fix: 附注号被当成数值列，货币资金读出来是 1

判据改成「整列都是没有千分位、没有小数点的小整数」。
按表头推列边界时，(五)52 这类附注号会聚成一个真的列。
```

---

## 五、上手：第一次要跑什么

### 三条命令（所有人都要先跑）

```bash
git clone https://github.com/zhoulie6220/finance_ai_competition.git
cd finance_ai_competition/backend
python -m venv .venv && .venv\Scripts\activate          # Windows
python -m pip install -r requirements.lock.txt          # 用 lock，不是 requirements.txt
python scripts/init_db.py --force                       # 建库 + 种子数据
```

> ⚠ 用 `requirements.lock.txt` 而不是 `requirements.txt`。后者只有直接依赖，
> 版本范围是开的——三个人装出三个版本的环境，报错还各不相同。

```bash
python -m pytest                                        # 全过再动手
```

然后起服务，**先看一遍再写代码**：

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
# 另开一个终端
cd ../frontend && npm install && npm run dev
```

浏览器开 `http://localhost:5173` → 选「宝钢股份（2015–2024）」→ 点
「看财务事实趋势」→ **点开表格里任意一个数字**，看到公式、入参和年报第 118 页。
**这就是你要往上长的那条线。**

> ⚠ 前端**必须 `npm install`**。ECharts 是新加的依赖，不装的话
> `npm run dev` 能起来，但一进页面就白屏——而且终端里没有一行报错。

### 分角色

**甲**：`backend/app/parsing/` 与 `engine/checks.py`

```bash
cd backend
python scripts/parse_reports.py --source "<年报根目录>" --force
python -m pytest tests/unit/parsing
```

**乙**：`backend/app/agents/`、`app/skills/`、`engine/index.py`

```bash
# 先加一个 Tool 或 Skill 跑通，再往大里做
python -m pytest tests/unit/agents tests/unit/engine
```

**丙**：`frontend/src/`

```bash
cd frontend && npm run dev
npm run build      # 提交前必跑，tsc 会挡住类型错误
```

---

## 六、用 AI 写代码的提示

**1. 让 AI 先读 `CLAUDE.md`。** 它写的是这个仓库特有的坑（附注号、负号、
`display_text`、JSON 列在仓储层转……），而这些坑**每一条都真的翻过车**。
不读的话，AI 会写出「看起来对、实际会静默错」的代码，然后你花半天才发现。

**2. 问「为什么」而不是「怎么写」。** 这个项目的代码注释里写了大量
「为什么不能那么写」，就是因为那些错法都不报错。让 AI 解释它为什么这么改，
比让它直接改完更省时间。

**3. 改完必跑这三条：**

```bash
cd backend
python -m pytest -q                                    # 测试
python scripts/export_schemas.py --check               # 契约同步
python scripts/gen_data_contract_doc.py --check
```

第二条尤其重要：**改了 `app/schemas/` 却忘了重新导出，前端类型会静默过期**——
字段拼错时 `npm run build` 不报错，只是页面上那个字段永远是空的。

**4. 别让 AI 动冻结的接缝。** `CLAUDE.md` 里列了 12 个已冻结的文件。
重写的代价不是「多写一遍」，而是同一个位置出现两套并存的约定——
一边发 `step.finished`、另一边发 `step.succeeded`，
前端只能显示一半，**而且两边都不报错**。

**5. AI 写的注释也要是中文**，那是对队友说的话。英文注释在评审那里也要扣分
（比赛是国内赛事）。`CLAUDE.md` 里有这条约定，以及为什么它重要。

---

## 七、每天收工前

```bash
git add -A
git commit -m "feat: ..."
git push origin feat-xxx
```

**别攒着。** 攒三天再提交，等于把三天的冲突挤到一个下午解决。
