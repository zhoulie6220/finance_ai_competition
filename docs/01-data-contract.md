# 数据契约

本文件是**人类可读版本**的数据契约。机读版本在 `backend/app/schemas/`（Pydantic v2），
二者必须同步；前端类型由 `backend/scripts/export_schemas.py` 从机读版本自动生成。

> 谁在依赖这份契约：后端的表结构、计算引擎的入参、前端的列头、导出报告的字段名。
> **契约冻结之前不要并行开工**——否则一方按 `value` 落库、另一方按 `amount` 渲染，
> 返工成本远大于等这一份文档定稿。

---

## 一、十个契约字段

任何进入系统的财务数字都必须齐备这十项。缺任何一项，它就不能被称为「已验证事实」。

| 契约字段 | 数据库列 | 含义 | 约束 |
|---|---|---|---|
| `metric` | `metric_key` | 指标键，如 `revenue` | 必须存在于字段字典 |
| `value` | `value_millions` | 数值，统一换算为**百万元** | 缺失时为 `NULL`，**绝不为 0** |
| `unit` | `unit` | 展示单位 | 默认「百万元」 |
| `period` | `period` | 报告期，如 `2024` / `2024H1` | 不得为空 |
| `scope` | `scope` | 会计口径：`consolidated` / `parent` | 默认合并口径 |
| `source_file` | `source_file` | 来源文件名 | 不得为空 |
| `source_page` | `source_page` | 物理页序 | 必须 > 0 |
| `source_text` | `source_text` | 原文片段 | 去空白后不得为空 |
| `confidence` | `confidence` | 置信度 0–1 | — |
| `status` | `status` | `validated` / `needs_review` / `not_found` / `rejected` | 见下 |

**为什么对外叫 `value`、库里叫 `value_millions`**：库列名要自带单位语义，否则半年后
没人记得这个 `value` 是元还是百万元。映射只发生在 repository 层，不要散落到业务代码。

**金额一律用 `Decimal`，出网序列化为字符串**。JSON 的 number 是 IEEE 754 双精度，
`12,345,678,901.23` 往返一次就可能变成 `12345678901.229998`。财务数据不允许这种误差。

---

## 二、三条硬规则

这三条**已经落到数据库的 CHECK 约束上**，不是文档里的口号。写不进去的东西，
就是真的写不进去。

### 规则一：无来源不得进已验证

`status='validated'` 时，`value`、`source_text`、`source_page`、`unit`、`period`、
`scope` 必须全部齐备。想让一个没有出处的数字进来，只能把它标成 `needs_review`——
而所有进入指数与估值的查询只读 `v_fact_verified` 视图，那里只暴露 `validated` 行。

### 规则二：缺失即无行

不设默认值，绝不插补 0。某个指标某年没有，就是**没有那一行**。
「未找到」由 `v_fact_grid` 视图左连接生成，只在前端展示。

> 为什么不能在代码里靠自觉：填写 0 看起来是最省事的「处理缺失」方式，而且不报错。
> 一旦某个环节填了 0，毛利率会变成 -100%，现金转化率会变成 0，而所有校验都会通过。

### 规则三：不可比必须写原因

`comparable = 0` 时 `incomparable_reason` 不得为空。可选值：
`mna`（并购）、`restructuring`、`asset_injection`、`scope_change`、`restatement`、
`policy_change`、`industry_cycle`、`other`。

不可比的年度不进指数扣分、不进正常化中枢，但**整行保留**——敏感性分析还要用它。

---

## 三、`period_kind`：不含 `prior`

取值只有 `current` / `instant` / `opening` / `average`，描述的是**数值本身的性质**，
不是它出现在哪份报告里。

FY2023 的数字，无论在 2023 年报的「本期」栏还是 2024 年报的「上期」栏，都是同一笔
经济事实：`period='2023'`、`period_kind='current'`，**存一行**。

> ⚠ 如果给「上期」也留一个 kind，同一笔事实就会按两种 kind 各存一行。
> 任何 `SUM` / `AVG` 聚合都会重复计算，而且**不会报错**——营收会平白多出一年。

同一笔事实的**多个来源**（三张主表 / 主要指标表 / 附注 / 正文，精度往往不同）记在
`fact_observation` 表：每次出现一条观测，交叉校验后把被采纳的标为 `adopted` 并回填
`fact_id`，其余必须写明未采纳理由。这部分是「年报里同一个数字有好几个版本」的解法。

---

## 四、字段字典

字段字典是 **PDF 行名 → 字段键** 映射的唯一依据。**漏一个别名，那个字段就解析不出来
并落到 `needs_review`**；而写错一个排除词，就会把「营业外收入」映射成「营业收入」——
两者都是数字，不会触发任何报错。

机读定义在 `backend/app/data/seed/0001_metric_definitions.sql`，下面的表格由
`python scripts/gen_data_contract_doc.py` 从该文件生成，不要手改表格内容。

**要改字典走 CSV 往返**，不要去手改 SQL 里嵌的 JSON 数组——中括号、逗号、引号全挤在
一行，改错一个引号不会报错，只会让那个字段静默失效：

```bash
cd backend
python scripts/dict_csv.py --export   # 导出 backend/app/data/metric_dictionary.csv
                                      # 用 Excel 打开，多值列用 | 分隔，一格一个
python scripts/dict_csv.py --import   # 读回来。先校验后写入，任何一条不通过就整体拒绝
```

`--import` 是**先校验、后写入**：候选内容先在内存库里加载并跑一遍全部规则，有问题就
原样保留旧文件，并指出是 Excel 的第几行。

<!-- BEGIN GENERATED: metrics -->

#### 利润表（28 项）

| 字段键 | 标准名称 | 类型 | 单位 | 派生 | 非经常 | 年报行名别名 | 排除词 |
|---|---|---|---|:--:|:--:|---|---|
| `total_revenue` | 营业总收入 | 期间 | currency |  |  | 营业总收入、一、营业总收入 | 营业总收入预算、营业总收入预测、营业总收入增长率 |
| `revenue` | 营业收入 | 期间 | currency |  |  | 营业收入、主营业务收入 | 营业收入预测、营业收入目标、营业收入占比、营业收入增长率、营业总收入 |
| `operating_cost` | 营业成本 | 期间 | currency |  |  | 营业成本、主营业务成本 | 营业成本率、营业成本预算、单位营业成本、营业总成本、二、营业总成本 |
| `gross_profit` | 毛利 | 期间 | currency | ✓ |  | — | — |
| `taxes_and_surcharges` | 税金及附加 | 期间 | currency |  |  | 税金及附加 | — |
| `selling_expense` | 销售费用 | 期间 | currency |  |  | 销售费用 | 销售费用率 |
| `admin_expense` | 管理费用 | 期间 | currency |  |  | 管理费用 | 管理费用率 |
| `rd_expense` | 研发费用 | 期间 | currency |  |  | 研发费用 | 研发费用率、研发投入、研发投入总额 |
| `finance_expense` | 财务费用 | 期间 | currency |  |  | 财务费用 | 利息费用、利息收入、财务费用率 |
| `interest_expense` | 利息费用 | 期间 | currency |  |  | 利息费用、利息支出 | 利息收入、财务费用、利息保障倍数 |
| `interest_income` | 利息收入 | 期间 | currency |  |  | 利息收入 | 利息费用、财务费用、投资收益 |
| `operating_profit` | 营业利润 | 期间 | currency |  |  | 营业利润、三、营业利润 | 营业利润率、营业利润增长率、营业利润预测 |
| `profit_before_tax` | 利润总额 | 期间 | currency |  |  | 利润总额、四、利润总额 | 利润总额增长率、利润总额预算、利润总额预测 |
| `income_tax` | 所得税费用 | 期间 | currency |  |  | 所得税费用、减：所得税费用 | 所得税税率、实际税率 |
| `net_income` | 净利润 | 期间 | currency |  |  | 净利润、五、净利润 | 净利润率、净利润增长率、净利润预测、归属于母公司 |
| `net_income_parent` | 归属于母公司股东的净利润 | 期间 | currency |  |  | 归属于母公司所有者的净利润、归属于母公司股东的净利润、归属于上市公司股东的净利润 | 归母净利润率、归母净利润增长率、归母净利润预测 |
| `minority_interest_pl` | 少数股东损益 | 期间 | currency |  |  | 少数股东损益 | — |
| `non_gaap_net_income` | 扣除非经常性损益后的净利润 | 期间 | currency |  |  | 扣除非经常性损益后的净利润、归属于上市公司股东的扣除非经常性损益的净利润、扣非净利润 | 非经常性损益、扣非净利润率、扣非净利润增长率 |
| `non_recurring_gain_loss` | 非经常性损益 | 期间 | currency |  | ✓ | 非经常性损益、非经常性损益合计 | 扣非净利润、扣除非经常性损益后的净利润 |
| `government_grant` | 政府补助 | 期间 | currency |  | ✓ | 政府补助、计入当期损益的政府补助 | 政府补助递延收益、政府补助退回、补助收入确认 |
| `asset_disposal_gain_loss` | 资产处置收益 | 期间 | currency |  | ✓ | 资产处置收益、资产处置损益 | 固定资产处置、资产处置收益率、营业外收入 |
| `impairment_loss` | 资产减值损失 | 期间 | currency |  | ✓ | 资产减值损失 | 信用减值损失、商誉减值、资产减值准备 |
| `credit_impairment_loss` | 信用减值损失 | 期间 | currency |  | ✓ | 信用减值损失 | 资产减值损失、坏账准备、预期信用损失 |
| `goodwill_impairment` | 商誉减值损失 | 期间 | currency |  | ✓ | 商誉减值损失 | 商誉账面价值、资产减值损失 |
| `fair_value_gain_loss` | 公允价值变动收益 | 期间 | currency |  | ✓ | 公允价值变动收益、公允价值变动损益 | 其他综合收益、交易性金融资产、公允价值计量 |
| `investment_income` | 投资收益 | 期间 | currency |  | ✓ | 投资收益 | 投资活动现金流、长期股权投资、其他收益 |
| `ebit` | 息税前利润 | 期间 | currency | ✓ |  | — | — |
| `ebitda` | 息税折旧摊销前利润 | 期间 | currency | ✓ |  | — | — |

#### 资产负债表（23 项）

| 字段键 | 标准名称 | 类型 | 单位 | 派生 | 非经常 | 年报行名别名 | 排除词 |
|---|---|---|---|:--:|:--:|---|---|
| `total_assets` | 资产总计 | 时点 | currency |  |  | 资产总计、资产总额 | 总资产周转率、资产负债率、资产总额增长率 |
| `total_liabilities` | 负债合计 | 时点 | currency |  |  | 负债合计、负债总额 | 资产负债率、有息负债、流动负债合计 |
| `total_equity` | 所有者权益合计 | 时点 | currency |  |  | 所有者权益合计、股东权益合计、所有者权益(或股东权益)合计 | 归属于母公司 |
| `equity_parent` | 归属于母公司所有者权益合计 | 时点 | currency |  |  | 归属于母公司所有者权益合计、归属于母公司股东权益合计、归属于上市公司股东的所有者权益 | 少数股东权益、股东权益合计、净资产收益率 |
| `minority_interest` | 少数股东权益 | 时点 | currency |  |  | 少数股东权益 | — |
| `cash_and_equivalents` | 货币资金 | 时点 | currency |  |  | 货币资金、库存现金及现金等价物 | 现金净增加额、现金流量净额 |
| `accounts_receivable` | 应收账款 | 时点 | currency |  |  | 应收账款 | 应收账款周转率、应收票据及应收账款、坏账准备 |
| `notes_and_ar` | 应收票据及应收账款 | 时点 | currency |  |  | 应收票据及应收账款 | 应收账款、应收票据、应收账款周转率 |
| `notes_receivable` | 应收票据 | 时点 | currency |  |  | 应收票据 | 应收票据及应收账款、应收款项融资 |
| `inventory` | 存货 | 时点 | currency |  |  | 存货 | 存货周转率、存货跌价准备、存货占比 |
| `contract_liabilities` | 合同负债 | 时点 | currency |  |  | 合同负债、预收款项 | 合同资产、合同负债率、负债合同 |
| `accounts_payable` | 应付账款 | 时点 | currency |  |  | 应付账款 | 应付票据及应付账款、应付账款周转率 |
| `ppe` | 固定资产 | 时点 | currency |  |  | 固定资产 | 固定资产周转率、固定资产原值、固定资产减值准备 |
| `cip` | 在建工程 | 时点 | currency |  |  | 在建工程 | 工程物资 |
| `goodwill` | 商誉 | 时点 | currency |  |  | 商誉 | 商誉减值准备、商誉占比、商誉评估 |
| `short_term_borrowing` | 短期借款 | 时点 | currency |  |  | 短期借款 | — |
| `long_term_borrowing` | 长期借款 | 时点 | currency |  |  | 长期借款 | — |
| `bonds_payable` | 应付债券 | 时点 | currency |  |  | 应付债券 | — |
| `lease_liability` | 租赁负债 | 时点 | currency |  |  | 租赁负债 | — |
| `interest_bearing_debt` | 有息负债合计 | 时点 | currency | ✓ |  | — | — |
| `net_debt` | 净债务 | 时点 | currency | ✓ |  | — | — |
| `operating_working_capital` | 经营性营运资本 | 时点 | currency | ✓ |  | — | — |
| `diluted_shares` | 稀释后总股本 | 时点 | shares |  |  | 稀释后总股本 | 总股本、基本每股收益对应的股本 |

#### 现金流量表（9 项）

| 字段键 | 标准名称 | 类型 | 单位 | 派生 | 非经常 | 年报行名别名 | 排除词 |
|---|---|---|---|:--:|:--:|---|---|
| `cfo` | 经营活动产生的现金流量净额 | 期间 | currency |  |  | 经营活动产生的现金流量净额、经营活动现金流量净额 | 投资活动产生的现金流量净额、筹资活动产生的现金流量净额、经营现金流率 |
| `cfi` | 投资活动产生的现金流量净额 | 期间 | currency |  |  | 投资活动产生的现金流量净额 | 经营活动产生的现金流量净额、筹资活动产生的现金流量净额 |
| `cff` | 筹资活动产生的现金流量净额 | 期间 | currency |  |  | 筹资活动产生的现金流量净额 | 经营活动产生的现金流量净额、投资活动产生的现金流量净额 |
| `depreciation_amortization` | 固定资产折旧、油气资产折耗、生产性生物资产折旧 | 期间 | currency |  |  | 固定资产折旧、折旧与摊销、固定资产折旧、油气资产折耗、生产性生物资产折旧 | 累计折旧 |
| `amortization_intangible` | 无形资产摊销 | 期间 | currency |  |  | 无形资产摊销、使用权资产折旧 | 累计摊销 |
| `capex` | 购建固定资产、无形资产和其他长期资产支付的现金 | 期间 | currency |  |  | 购建固定资产、无形资产和其他长期资产支付的现金、购建固定资产、无形资产和其他长期资产所支付的现金 | 固定资产原值、固定资产账面价值、资本化研发支出 |
| `cash_begin` | 期初现金及现金等价物余额 | 时点 | currency |  |  | 期初现金及现金等价物余额 | 期末现金及现金等价物余额 |
| `cash_end` | 期末现金及现金等价物余额 | 时点 | currency |  |  | 期末现金及现金等价物余额 | 现金净增加额、现金流量净额、货币资金 |
| `cash_net_increase` | 现金及现金等价物净增加额 | 期间 | currency |  |  | 现金及现金等价物净增加额 | 期末现金及现金等价物余额、经营现金流净额 |

#### 股本与每股（11 项）

| 字段键 | 标准名称 | 类型 | 单位 | 派生 | 非经常 | 年报行名别名 | 排除词 |
|---|---|---|---|:--:|:--:|---|---|
| `eps_basic` | 基本每股收益 | 期间 | currency |  |  | 基本每股收益 | 稀释每股收益、基本每股收益率、每股收益增长率 |
| `eps_diluted` | 稀释每股收益 | 期间 | currency |  |  | 稀释每股收益 | 基本每股收益、稀释每股收益率 |
| `bvps` | 每股净资产 | 时点 | currency |  |  | 归属于上市公司股东的每股净资产、每股净资产 | — |
| `ebit_margin` | EBIT 利润率 | 比率 | percent | ✓ |  | — | — |
| `ebitda_margin` | EBITDA 利润率 | 比率 | percent | ✓ |  | — | — |
| `gross_margin` | 毛利率 | 比率 | percent | ✓ |  | — | — |
| `net_margin` | 净利率 | 比率 | percent | ✓ |  | — | — |
| `roe` | 净资产收益率 | 比率 | percent | ✓ |  | — | — |
| `roic` | 投入资本回报率 | 比率 | percent | ✓ |  | — | — |
| `cash_conversion` | 现金转化率 | 比率 | percent | ✓ |  | — | — |
| `nonrecurring_share` | 非经常性损益占比 | 比率 | percent | ✓ |  | — | — |

#### 披露事项（2 项）

| 字段键 | 标准名称 | 类型 | 单位 | 派生 | 非经常 | 年报行名别名 | 排除词 |
|---|---|---|---|:--:|:--:|---|---|
| `audit_opinion` | 审计意见类型 | 文本 | text |  |  | 审计意见、审计意见类型 | 内部控制审计意见、审计费用、审计委员会 |
| `accounting_policy_change` | 会计政策变更 | 文本 | text |  |  | 会计政策变更 | 会计估计变更、前期差错更正、会计政策和会计估计 |

#### 钢铁专属（10 项）

| 字段键 | 标准名称 | 类型 | 单位 | 派生 | 非经常 | 年报行名别名 | 排除词 |
|---|---|---|---|:--:|:--:|---|---|
| `steel_output` | 钢材产量 | 期间 | ton |  |  | 钢材产量 | 粗钢产量、生铁产量、钢材销量、钢材产能、钢材销售收入 |
| `crude_steel_output` | 粗钢产量 | 期间 | ton |  |  | 粗钢产量 | 钢材产量、钢材销量、粗钢产能 |
| `steel_sales_volume` | 钢材销量 | 期间 | ton |  |  | 钢材销量、销售量 | 粗钢产量、钢材产量、钢材产能、钢材销售收入 |
| `steel_price_avg` | 钢材平均售价 | 比率 | currency | ✓ |  | — | — |
| `steel_spread` | 吨钢原料差价 | 比率 | currency | ✓ |  | 吨钢原料差价 | 吨钢毛利、吨钢 EBITDA、吨钢成本 |
| `steel_gross_profit_per_ton` | 吨钢毛利 | 比率 | currency | ✓ |  | 吨钢毛利 | 吨钢原料差价、吨钢 EBITDA、吨钢成本 |
| `steel_ebitda_per_ton` | 吨钢 EBITDA | 比率 | currency | ✓ |  | 吨钢 EBITDA | 吨钢毛利、吨钢原料差价、EBITDA 利润率 |
| `effective_capacity` | 有效产能 | 时点 | ton |  |  | 有效产能、钢材有效产能、粗钢有效产能 | 设计产能、产能利用率、产能置换 |
| `capacity_utilization` | 产能利用率 | 比率 | percent | ✓ |  | 产能利用率 | 设计产能、有效产能、产能利用率目标 |
| `crude_steel_capacity` | 粗钢产能 | 时点 | ton |  |  | 粗钢产能、钢铁产能 | 设计产能、有效产能 |

#### 能源专属（5 项）

| 字段键 | 标准名称 | 类型 | 单位 | 派生 | 非经常 | 年报行名别名 | 排除词 |
|---|---|---|---|:--:|:--:|---|---|
| `coal_output` | 原煤产量 | 期间 | ton |  |  | 原煤产量、商品煤产量、煤炭产量 | 煤炭销量、原煤销量 |
| `coal_sales_volume` | 商品煤销量 | 期间 | ton |  |  | 商品煤销量、煤炭销量、销售量 | 原煤产量、商品煤产量 |
| `coal_price_avg` | 吨煤平均售价 | 比率 | currency | ✓ |  | 吨煤售价、煤炭平均售价 | — |
| `ton_coal_gross_margin` | 吨煤毛利 | 比率 | currency | ✓ |  | 吨煤毛利 | 吨煤原料差价、吨煤成本 |
| `safety_production_expense` | 安全生产费用 | 期间 | currency |  |  | 安全生产费用、专项储备 | — |

#### 其他（1 项）

| 字段键 | 标准名称 | 类型 | 单位 | 派生 | 非经常 | 年报行名别名 | 排除词 |
|---|---|---|---|:--:|:--:|---|---|
| `environmental_protection_expense` | 环保投入 | 期间 | currency |  |  | 环保投入、环保支出、环保费用 | — |

合计 **89** 个字段。

#### 例句锚点（48/89 项已填，其中占位符句 48 项）

| 字段键 | 例句 | 来源 |
|---|---|---|
| `total_revenue` | 本年度营业总收入为【数值】元。 | 占位符句 |
| `revenue` | 本报告期实现营业收入【数值】元，同比【上升/下降】【数值】%。 | 占位符句 |
| `operating_cost` | 本报告期营业成本为【数值】元。 | 占位符句 |
| `gross_profit` | 本年度实现毛利【数值】元，毛利率为【数值】%。 | 占位符句 |
| `finance_expense` | 本年度财务费用为【数值】元。 | 占位符句 |
| `interest_expense` | 本年度利息费用为【数值】元。 | 占位符句 |
| `interest_income` | 本年度利息收入为【数值】元。 | 占位符句 |
| `operating_profit` | 本年度营业利润为【数值】元。 | 占位符句 |
| `profit_before_tax` | 本年度利润总额为【数值】元。 | 占位符句 |
| `net_income` | 本报告期实现净利润【数值】元。 | 占位符句 |
| `net_income_parent` | 归属于母公司股东的净利润为【数值】元。 | 占位符句 |
| `non_gaap_net_income` | 扣除非经常性损益后的净利润为【数值】元。 | 占位符句 |
| `non_recurring_gain_loss` | 归属于上市公司股东的非经常性损益为【数值】元。 | 占位符句 |
| `government_grant` | 本年度计入当期损益的政府补助为【数值】元。 | 占位符句 |
| `asset_disposal_gain_loss` | 本年度资产处置收益为【数值】元。 | 占位符句 |
| `impairment_loss` | 本年度确认资产减值损失【数值】元。 | 占位符句 |
| `credit_impairment_loss` | 本年度确认信用减值损失【数值】元。 | 占位符句 |
| `goodwill_impairment` | 本年度计提商誉减值损失【数值】元。 | 占位符句 |
| `fair_value_gain_loss` | 本年度公允价值变动收益为【数值】元。 | 占位符句 |
| `investment_income` | 本年度投资收益为【数值】元。 | 占位符句 |
| `total_assets` | 报告期末资产总额为【数值】元。 | 占位符句 |
| `total_liabilities` | 报告期末负债总额为【数值】元。 | 占位符句 |
| `equity_parent` | 报告期末归属于母公司股东的权益为【数值】元。 | 占位符句 |
| `accounts_receivable` | 期末应收账款账面余额为【数值】元。 | 占位符句 |
| `notes_and_ar` | 期末应收票据及应收账款为【数值】元。 | 占位符句 |
| `inventory` | 期末存货账面余额为【数值】元。 | 占位符句 |
| `contract_liabilities` | 期末合同负债为【数值】元。 | 占位符句 |
| `accounts_payable` | 期末应付账款为【数值】元。 | 占位符句 |
| `ppe` | 期末固定资产账面价值为【数值】元。 | 占位符句 |
| `goodwill` | 期末商誉账面价值为【数值】元。 | 占位符句 |
| `cfo` | 经营活动产生的现金流量净额为【数值】元。 | 占位符句 |
| `cfi` | 投资活动产生的现金流量净额为【数值】元。 | 占位符句 |
| `cff` | 筹资活动产生的现金流量净额为【数值】元。 | 占位符句 |
| `capex` | 本年度购建固定资产、无形资产和其他长期资产支付的现金为【数值】元。 | 占位符句 |
| `cash_end` | 期末现金及现金等价物余额为【数值】元。 | 占位符句 |
| `cash_net_increase` | 现金及现金等价物净增加额为【数值】元。 | 占位符句 |
| `eps_basic` | 本年度基本每股收益为【数值】元/股。 | 占位符句 |
| `eps_diluted` | 本年度稀释每股收益为【数值】元/股。 | 占位符句 |
| `steel_output` | 本年度钢材产量为【数值】万吨。 | 占位符句 |
| `crude_steel_output` | 本年度粗钢产量为【数值】万吨。 | 占位符句 |
| `steel_sales_volume` | 本年度钢材销量为【数值】万吨。 | 占位符句 |
| `steel_spread` | 本年度吨钢原料差价为【数值】元/吨。 | 占位符句 |
| `steel_gross_profit_per_ton` | 本年度吨钢毛利为【数值】元/吨。 | 占位符句 |
| `steel_ebitda_per_ton` | 本年度吨钢 EBITDA 为【数值】元/吨。 | 占位符句 |
| `effective_capacity` | 报告期钢材有效产能为【数值】万吨。 | 占位符句 |
| `capacity_utilization` | 本年度粗钢产能利用率为【数值】%。 | 占位符句 |
| `audit_opinion` | 会计师事务所对公司本年度财务报告出具了【审计意见类型】。 | 占位符句 |
| `accounting_policy_change` | 本年度因会计政策变更对比较数据进行追溯调整。 | 占位符句 |

> 仍有 **41** 个字段没有例句锚点。占位符句必须在上传真实年报后逐条替换为原文，并把 `example_source` 改为 `annual_report`——在此之前它不能作为任何结论的证据。

<!-- END GENERATED: metrics -->

### 行名映射的匹配语义

**先说清楚匹配规则，否则排除词会写出相反的效果。**

| 规则 | 语义 |
|---|---|
| 别名匹配 | 归一化后的 PDF 行名与别名**精确相等**（不是包含） |
| 排除词 | 同样是**精确相等**。命中则本字段不参与该行的竞争 |
| 多个字段同时命中 | 视为字典缺陷，由 `test_metric_dictionary.py::test_no_alias_shared_between_metrics` 拦截 |
| 无字段命中 | 落 `needs_review`，**不得由 LLM 自行猜测字段** |
| 跨行业重名 | 先按 `project.industry` 过滤候选字段再匹配（如「销售量」钢企煤企都有） |

> ⚠ 排除词为什么必须是精确匹配而非包含匹配：`notes_and_ar`（应收票据及应收账款）
> 的别名**包含** `应收账款`，而 `应收账款` 又是它的排除词。若用包含匹配，它会被
> 自己的排除词永远挡住。精确匹配下「应收账款」不等于「应收票据及应收账款」，
> 两者各自命中各自的字段。

### 会计/金融同学需要逐条审校的内容

按 `方案选择.docx` 第 8 条，每个字段必须确认以下六项：

1. **标准名称**——已填，确认用词是否符合团队口径
2. **PDF 行名别名**——已填初稿，**这是最需要补全的一列**。各家公司写法不一
   （「营业收入」/「营业总收入」/「一、营业总收入」），年报里出现过的写法都要收进来
3. **适用报表**——已填，确认
4. **排除词**——已按 `exclusion_terms_and_sentence.docx` 填入 48 个字段。三类来源：
   - **同名量纲变体**：`net_income` 排除「净利润率」「净利润增长率」「净利润预测」
   - **相邻行名**：`interest_expense` 排除「利息收入」；`impairment_loss` 排除「信用减值损失」
   - **合计行 vs 明细行**：`accounts_receivable` 排除「应收票据及应收账款」
     ——那是含本科目在内的合并列示行，两边都收会重复计算
5. **合并/母公司口径**——见 `scope_note` 列，确认本字段在年报中的惯常口径
6. **真实年报例句**——见下方「例句锚点」一节，从样例年报里摘一句原文，
   作为别名维护的锚点，也便于评委核对

> **例句来源标记是硬约束。** `example_source='synthetic_example'` 表示该句仍是
> 带【数值】的标准句，**只供解析器回归测试，不得在界面或报告里当作年报原文展示**。
> 摘录真实原文后须同步改为 `annual_report`，由
> `test_annual_report_examples_are_not_placeholders` 反向校验。

---

## 五、其他数据契约

除财务事实外，系统还有几组结构化的契约，定义同在 `backend/app/schemas/`：

| 契约 | 用途 | 关键约束 |
|---|---|---|
| `Claim` / `ClaimMatch` | MD&A 可验证主张与其匹配结果 | 不可验证的主张必须标 `background_only`，不得进入评分；判定不可比时必须写原因 |
| `DiagnosisRun` | 叙事—财务一致性诊断指数 | `grade=insufficient` 时不得输出分值，且必须写明原因 |
| `NormalizationRun` | 周期正常化 | 失败时**不得输出任何中枢值**；交叉验证项不得影响 DCF |
| `ValuationRun` | DCF 与相对估值 | 每个参数必须标记来源；周期行业必须引用一次已正常化的运行 |
| `Evidence` | 多态证据指针 | 任何结论都能回溯到「文件 → 页码 → 原文」 |
| `ToolCall` / `LlmCall` | 工具与模型调用记录 | 记录 `deterministic` 标志，区分「程序算的」与「模型理解的」 |
| `RunManifest` | 可复现凭据 | 代码版本 + 依赖锁哈希 + 输入 sha256 + prompt 哈希 + 规则版本 + 种子 |

赛事要求提交源码时须包含「智能体编排框架、Tool、Prompt、Skill、MCP、数据处理、
日志记录」七类模块，`README.md` 里有目录映射表。
