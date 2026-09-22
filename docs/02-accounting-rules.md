# 02 · 会计硬规则与校验清单

**本文是会计/金融方向同学负责并签字的文档。**
规则变更必须走本文的修订流程，并同步 `docs/01-data-contract.md` 与种子数据。

> **当前生效版本**：`accounting_signoff_v1`（2026-09-22）
> 源文件为仓库根目录的 `accounting_signoff_v1.docx`，本文是它的**机读化落地记录**：
> 每一条都写明「在哪一行代码里真的会被拦住」，而不只是「原则上应当如此」。

---

## 一、总口径

| 项目 | 决定 |
|---|---|
| 主公司 | 宝钢股份 |
| 可比公司 | 华菱钢铁、首钢股份 |
| 细分行业 | 钢铁——黑色金属冶炼和压延加工，长流程/综合钢企（**不选煤炭**） |
| 主公司年报 | 2015—2024 共 10 份 |
| 可比公司年报 | 2 家，每家至少 3 份 |
| 正常化主窗口 | 2017—2024（8 个完整年度） |
| 回退窗口 | 2015—2024（10 年） |
| DCF 正常化核心指标 | 收入加权的周期中位 EBIT margin |
| 历史 EBIT 基础口径 | `利润总额 + 利息费用 − 利息收入` |
| 资产减值 | 逐年判断，不机械剔除 |
| 无法由原始材料支持的判断 | 一律进 `needs_review`，**不得由模型自动补全** |

「10 份年报轻解析 + 最近 3 份精细解析」：主公司 2015—2024 全部 10 份只做营业收入、
利润总额、利息费用/利息收入的**轻解析**（供 8 年窗口使用）；最近 3 份做完整精细解析。

---

## 二、字段键名（A-1）

13 项改名与 1 项跨年度主字段，**旧键名只保留在迁移映射表中，不再写入新数据**。

| 旧键名 | 新键名 |
|---|---|
| `total_profit` | `profit_before_tax` |
| `net_profit` | `net_income` |
| `net_profit_attr_parent` | `net_income_parent` |
| `deducted_net_profit` | `non_gaap_net_income` |
| `financial_expense` | `finance_expense` |
| `equity_attr_parent` | `equity_parent` |
| `contract_liability` | `contract_liabilities` |
| `fixed_assets` | `ppe` |
| `asset_impairment` | `impairment_loss` |
| `nonrecurring_pl` | `non_recurring_gain_loss` |
| `gov_subsidy` | `government_grant` |
| `asset_disposal_gain` | `asset_disposal_gain_loss` |
| `ton_steel_gross_margin` | `steel_gross_profit_per_ton` |
| `trade_receivables` | `notes_and_ar`（A-3，跨年度主字段） |

**落地位置**

- 映射表：`metric_key_migration` 表，种子 `app/data/seed/0003_metric_key_migration.sql`
- 旧键名写不进事实表：`financial_fact.metric_key` 有外键指向 `metric_definition`，
  旧键名已不是那里的行 —— 由 `test_legacy_keys_cannot_be_written_as_facts` 盯着
- 表结构里也不许再有旧键名 —— 由 `test_no_live_column_is_named_after_a_legacy_key` 盯着
  （这条测试是被真实情况逼出来的：`normalization_year` 里曾留着 `total_profit`
  和 `financial_expense` 两列）

> **为什么值得为改名单建一张表**：改名不会报错。队友按早先的设计文档写出 `net_profit`，
> 只会得到「这个字段没有数据」——和「公司没披露这一项」看起来一模一样。
> 有了映射表，仓储层能报出「`net_profit` 已改名为 `net_income`（依据 A-1）」。

---

## 三、收入口径（A-2）

`revenue`（营业收入）与 `total_revenue`（营业总收入）**两个都抽，但只允许一个进主计算**。

- `revenue`：主分析字段
- `total_revenue`：父级 / 原始披露字段
- **两者不得相加**
- 两者数值相同时也分别保留来源
- 只披露营业总收入时，值映射到 `revenue`，并记 `mapped_from = 'total_revenue'`

收入增速、EBIT margin、DCF、营运资本计算**统一使用 `revenue`**。

**落地位置**：`financial_fact.mapped_from`（外键），
由 `test_mapped_from_records_the_total_revenue_fallback` 盯着。

> 为什么单列一列而不是靠 `source_row_label`：后者是自由文本的原始行名，
> 无法参与计算。没有这一列时，一组 `(revenue, 2024)` 数据**看不出**它是营业收入
> 还是营业总收入，聚合时就会把两者重复计入。

---

## 四、应收票据与应收账款（A-3）

```text
trade_receivables = notes_and_ar
```

采用 `notes_and_ar` 作为**跨年度主字段**：2018 年起年报以「应收票据及应收账款」
合并列示，2018 年之前拆成两行。跨年度做应收周转必须用同一个键，
否则会出现一次纯粹由列示方式变化造成的假跳变。

若原始年报同时披露拆分项，则保存 `accounts_receivable` 与 `notes_receivable` 作为明细，
但**不得与 `notes_and_ar` 重复计入营运资本**。

**落地位置**：字典的 `exclusion_terms` 互相排除（`notes_and_ar` 排除「应收账款」
「应收票据」；两个明细项排除「应收票据及应收账款」）。

---

## 五、吨钢毛利（A-4）

```text
吨钢毛利 =（钢材销售收入 − 钢材销售成本）÷ 钢材销售量
```

- 钢材销售成本包括公司披露口径下的**材料、人工、制造费用和折旧**
- **分母使用销售量，不使用产量**
- 如果只能得到「售价减原材料成本」，字段必须命名为 `steel_spread`，
  **不得命名为吨钢毛利**——两者差着一整个人工、制造费用与折旧，称为毛利会虚高约一倍

**落地位置**：字典中 `steel_gross_profit_per_ton` 与 `steel_spread` 互为排除词；
`rule_config['crosscheck.ton_steel_gross_margin.denominator'] = 'sales_volume'`。

---

## 六、产能利用率（A-5）

```text
产能利用率 = 产量 ÷ 有效产能
```

- 默认分母为 `effective_capacity`
- **粗钢产量只能匹配粗钢有效产能，钢材产量只能匹配钢材有效产能**
- 设计产能只作为补充字段（`crude_steel_capacity`），**不进入主指标**

**落地位置**：字典拆成 `crude_steel_output` / `crude_steel_effective_capacity`、
`steel_output` / `steel_effective_capacity` **四个独立字段**，而不是一个字段加一句说明。

> 混用粗钢与钢材分母不会报错，只会得到一个大于 100% 或明显偏低的利用率。
> 这类错误**只能靠字段本身挡住**，靠注释是挡不住的。

---

## 七、周期正常化阈值（A-6）

| 参数 | 最终值 | `rule_config` 键 |
|---|---:|---|
| 主窗口 | 8 年 | `normalization.preferred_years` |
| 主窗口最少可比年度 | 7 年 | `normalization.min_comparable_years` |
| 回退窗口 | 10 年 | `normalization.fallback_years` |
| 回退窗口最少可比年度 | 8 年 | `normalization.min_comparable_years_fallback` |
| **高低盈利阶段最低落差** | **2 个百分点** | `normalization.phase_min_spread` |
| EBIT 交叉口径偏离复核阈值 | 5% | `normalization.ebit_crosscheck_tolerance` |
| 交叉验证指标偏离复核阈值 | 20% | `normalization.crosscheck_agreement_tolerance` |

8 年窗口必须**同时存在**高盈利阶段和低盈利阶段。8 年不满足时切换 10 年；
10 年仍不满足时返回 `NORMALIZATION_INSUFFICIENT_DATA`。

**落地位置**：`app/engine/normalization.py::check_cycle_coverage`。
判定用**相对中枢的绝对落差**，不是分位数 —— 由
`test_phase_spread_in_config_matches_the_engine_default` 与
`test_quantile_phase_params_are_gone` 盯着。

> ⚠ **为什么删掉了分位数版参数**：分位数判定对任何窗口都恒为真 —— 总能找到
> 「最高的那个」和「最低的那个」年份。一个全部由平淡年份组成的窗口照样能通过校验，
> 然后这套平淡的 margin 会被当成「穿越周期的中枢」去做永续增长。
> 删掉参数不只是因为没用，而是因为**它看起来有用**：留着它，下一个人会照着它实现。

---

## 八、EBIT 与非经常性项目

### 8.1 基础 EBIT

```text
Reported EBIT    = 利润总额 + 利息费用 − 利息收入     ← 主 DCF 默认
Cross-check EBIT = 营业利润 + 财务费用
```

两者偏离超过 **5%** 时进入 `needs_review`。

### 8.2 调整规则

```text
Adjusted EBIT = Reported EBIT
              − 非核心收益 + 非核心损失
              + 经批准加回的一次性费用 − 经批准剔除的一次性收益
```

| 项目 | 默认处理 |
|---|---|
| 一次性政府补助 | 剔除 |
| 持续性经营补贴 | 可保留，需说明 |
| 非主营投资收益 | 剔除 |
| 核心联营业务收益 | 可保留，需说明 |
| 非主营公允价值收益 | 剔除 |
| 持续性主营套期保值 | 保留 |
| 一次性 / 投机性套期保值 | 剔除 |
| 非日常资产处置 | 剔除 |
| **资产减值** | **逐年判断，不机械剔除** |

**落地位置**：`rule_config` 的 `nonrecurring.*` 组。

### 8.3 资产减值

- 反映产能过剩、竞争力下降或长期盈利恶化的减值：**不加回**
- 有证据证明一次性、非重复、且不代表持续经营恶化的减值：**可加回**
- 报告 EBIT 与调整后 EBIT **必须同时展示**
- 会计确认之前，DCF 默认使用 **reported**；adjusted 只作敏感性分析

**落地位置**：`rule_config['normalization.default_ebit_variant'] = 'reported'`；
加回必须经 `ebit_adjustment` 留痕并获批准
（`normalization.impairment_addback_requires_evidence = 1`）。

---

## 九、诊断指数（A-7）

```text
I = 50 + 20H + 20C + 5R − 10P − 15Q     裁剪到 [0, 100]
```

变量均标准化为 0—1：

- `H` 历史主张兑现度
- `C` 当前 MD&A 与财务事实一致性
- `R` 风险披露完整度和可验证性
- `P` 模板化、模糊化、回避性表述惩罚
- `Q` 财务质量冲突惩罚

| 分级 | 含义 |
|---|---|
| `I ≥ 70` | 一致性较高 |
| `45 ≤ I < 70` | 中等，需关注 |
| `I < 45` | 一致性较低，建议人工复核 |

其他参数：

```text
主张覆盖率下限：0.60
最少可验证观测数：5
数值型主张方向/幅度冲突阈值：20 个百分点
```

20 个百分点**只用于带明确数值目标的主张**。方向性主张若实际方向相反，
直接标记为冲突，不做幅度判断。

**落地位置**：`rule_config` 的 `index.*` 组。

---

## 十、特殊年度与窗口

重大重组、资产注入、合并范围变化、前期重述、会计政策变更年度**先标记**
`non_comparable`；确认不可比后才从主中枢剔除，但**保留在敏感性分析中**。

> **不得为了得到完整周期而手工删除异常年度。**

**落地位置**：`financial_fact` 与 `normalization_year` 的 `incomparable_reason`
取值表（两处必须一致），由
`test_every_declared_incomparable_reason_is_writable` 与
`test_normalization_year_accepts_the_same_reasons` 盯着。

> 这两条测试抓到过一次真实分叉：Pydantic 放行 `asset_injection`，
> 而 `financial_fact` 的 CHECK 里没有它 —— 于是签字文档要求标记的「资产注入年度」
> **根本标不上**，那一年就静默留在了主中枢里，把周期中枢算高。

---

## 十一、真实年报例句（§6）

占位符**不能**作为正式年报证据。每条真实例句入库必须**同时**具备：

```text
example_sentence = 真实年报原句
example_source   = annual_report
example_file     = 实际 PDF 文件名
example_page     = 实际页码
```

年报 PDF 入库后**优先补齐**这 6 个字段：

```text
revenue   operating_profit   profit_before_tax
interest_expense   cfo   non_recurring_gain_loss
```

占位句只能标记为 `synthetic_example`，且**不得**计入「年报原文」的任何统计。

**落地位置**：`metric_definition` 的 CHECK 约束 ——
`example_source='annual_report'` 时强制要求 `example_file` 与 `example_page` 齐备，
与 `financial_fact` 的硬规则一同源。

---

## 十二、规则修改流程（A-8）

`rule_config` 的三类参数分级（列 `tier`），决定改一个参数需要谁点头：

| `tier` | 范围 | 变更要求 |
|---|---|---|
| `hard`（默认） | 字段口径、EBIT、周期窗口、阈值、减值、估值公式 | **发布前必须会计签字** |
| `soft` | 提示语、颜色、排序 | 可后续调整 |
| `model` | 模型名、Prompt 版本、温度、输出长度 | 需记录版本，**不属于会计口径** |

**不写 `tier` 的行一律按 `hard` 处理。** 这个默认方向是刻意的：
让参数「不算数」需要有人明确声明，而不是靠忘记写 `tier` 来偷偷降级。

> `model` 类参数不写在种子文件里，而是每次运行记进 `run_manifest`
> （模型名与版本、`prompt_registry_hash`、随机种子）。把 `deepseek-chat`、温度 0
> 这类值固化在种子文件里，等于给出一份「看起来已经定下来、实际没人确认过」的配置。

**修改规则时只改配置与年度标注，不修改核心计算算法。**

---

## 十三、工程验收清单（§7）

- [ ] 新字段键名全部生效，旧键名仅用于迁移
- [ ] `revenue` 与 `total_revenue` 不重复计算
- [ ] 应收票据及应收账款不与拆分项重复计入
- [ ] 吨钢毛利使用销售成本和销售量
- [ ] 产能利用率使用有效产能
- [ ] 8 年主窗口和 10 年回退可复现
- [ ] EBIT 偏离 5% 时进入 `needs_review`
- [ ] 诊断指数参数和构成项可解释
- [ ] 特殊年度不会被静默删除
- [ ] 所有真实例句均有 PDF、页码和 `annual_report` 来源
- [ ] API Key 不进入代码库和日志

---

## 十四、签字

| 角色 | 姓名 | 日期 |
|---|---|---|
| 会计 / 金融负责人 | | |
| 计算机负责人 | | |
| 项目负责人 | | |
