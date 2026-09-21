-- =============================================================================
-- 财务字段字典种子数据（能源钢铁行业）
--
-- 这是 docs/01-data-contract.md 的机读版本。metric_key 采用会计方向同学的
-- exclusion_terms_and_sentence.docx 口径，两边的键必须完全一致——同一个概念
-- 有两个名字，是「一方按 value 落库、另一方按 amount 渲染」的返工起点。
--
-- 约定：
--   aliases         年报表格里的原始行名，JSON 数组。会计同学增补即可，不必改代码。
--   exclusion_terms 行名命中这些词时**不得**映射到本字段，JSON 数组。见下方说明。
--   value_type      stock=时点（资产负债表） flow=期间（利润表/现金流量表）
--                   ratio=比率 text=文本（无对应数值）
--   is_nonrecurring 非经常性损益类，须单独列示并参与扣非净利润对照
--   is_derived      需由引擎计算取得，而非从报表直接读取
--   industry        行业专属指标；NULL 表示通用指标
--   example_source  annual_report=真实年报原文；synthetic_example=占位符标准句，
--                   **仅供解析器回归测试，不得当作真实证据**
--
-- 排除词的三类来源：
--   ① 同名量纲变体：'净利润' 排除 '净利润率' / '净利润增长率' / '净利润预测'
--   ② 相邻行名：    '利息费用' 排除 '利息收入'；'资产处置收益' 排除 '营业外收入'
--   ③ 合计行 vs 明细行：'应收账款' 排除 '应收票据及应收账款'（合并列示行含应收账款本身，
--      两边都收会重复计算）
-- 排除词漏一个，「营业成本率」「营业总成本」这类行就会被当成「营业成本」收下——
-- 两者都是数字，不会报错，只会让毛利率静默错成另一个量级。
-- =============================================================================

-- ⚠ 下面的数据块由 scripts/dict_csv.py 生成，不要手改。
--   要改字典：python scripts/dict_csv.py --export → 用 Excel 编辑
--            → python scripts/dict_csv.py --import
--   手改这里也能跑（init_db.py 照样加载），但下次 --import 会覆盖掉。
-- BEGIN GENERATED: metric_definitions
INSERT INTO metric_definition
  (metric_key, label_cn, aliases, exclusion_terms, statement, value_type, unit_kind, sign_convention, is_nonrecurring, is_derived, industry, parent_key, display_order, example_sentence, example_source, scope_note, note)
VALUES
  -- ---- 利润表
  ('total_revenue', '营业总收入', '["营业总收入", "一、营业总收入"]', '["营业总收入预算", "营业总收入预测", "营业总收入增长率"]', 'income', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 10, '本年度营业总收入为【数值】元。', 'synthetic_example', NULL, '利润表第一行。一般工商企业 营业总收入 = 营业收入，但两者是两行，聚合时只能用其一'),
  ('revenue', '营业收入', '["营业收入", "主营业务收入"]', '["营业收入预测", "营业收入目标", "营业收入占比", "营业收入增长率", "营业总收入"]', 'income', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, 'total_revenue', 11, '本报告期实现营业收入【数值】元，同比【上升/下降】【数值】%。', 'synthetic_example', NULL, '营业总收入下的「其中：」子项'),
  ('operating_cost', '营业成本', '["营业成本", "主营业务成本"]', '["营业成本率", "营业成本预算", "单位营业成本", "营业总成本", "二、营业总成本"]', 'income', 'flow', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 20, '本报告期营业成本为【数值】元。', 'synthetic_example', NULL, '⚠ 「营业总成本」是含税金及附加与四项期间费用的合计行，误映射过来会让毛利率变成营业利润率'),
  ('gross_profit', '毛利', '[]', NULL, 'income', 'flow', 'currency', 'positive_is_good', 0, 1, NULL, NULL, 21, '本年度实现毛利【数值】元，毛利率为【数值】%。', 'synthetic_example', NULL, '计算值 = 营业收入 − 营业成本'),
  ('taxes_and_surcharges', '税金及附加', '["税金及附加"]', NULL, 'income', 'flow', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 22, NULL, NULL, NULL, NULL),
  ('selling_expense', '销售费用', '["销售费用"]', '["销售费用率"]', 'income', 'flow', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 23, NULL, NULL, NULL, NULL),
  ('admin_expense', '管理费用', '["管理费用"]', '["管理费用率"]', 'income', 'flow', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 24, NULL, NULL, NULL, NULL),
  ('rd_expense', '研发费用', '["研发费用"]', '["研发费用率", "研发投入", "研发投入总额"]', 'income', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 25, NULL, NULL, NULL, '研发投入总额含资本化部分，大于费用化的研发费用'),
  ('finance_expense', '财务费用', '["财务费用"]', '["利息费用", "利息收入", "财务费用率"]', 'income', 'flow', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 26, '本年度财务费用为【数值】元。', 'synthetic_example', NULL, '含利息费用、利息收入、汇兑损益。「利息支出」明细在附注，不要与本科目混用'),
  ('interest_expense', '利息费用', '["利息费用", "利息支出"]', '["利息收入", "财务费用", "利息保障倍数"]', 'income', 'flow', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 27, '本年度利息费用为【数值】元。', 'synthetic_example', NULL, '★ Reported EBIT = 利润总额 + 利息费用 − 利息收入 的必需项'),
  ('interest_income', '利息收入', '["利息收入"]', '["利息费用", "财务费用", "投资收益"]', 'income', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 28, '本年度利息收入为【数值】元。', 'synthetic_example', NULL, '★ Reported EBIT = 利润总额 + 利息费用 − 利息收入 的必需项'),
  ('operating_profit', '营业利润', '["营业利润", "三、营业利润"]', '["营业利润率", "营业利润增长率", "营业利润预测"]', 'income', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 30, '本年度营业利润为【数值】元。', 'synthetic_example', NULL, 'Cross-check EBIT = 营业利润 + 财务费用'),
  ('profit_before_tax', '利润总额', '["利润总额", "四、利润总额"]', '["利润总额增长率", "利润总额预算", "利润总额预测"]', 'income', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 31, '本年度利润总额为【数值】元。', 'synthetic_example', NULL, '★ Reported EBIT 的起算项'),
  ('income_tax', '所得税费用', '["所得税费用", "减：所得税费用"]', '["所得税税率", "实际税率"]', 'income', 'flow', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 32, NULL, NULL, NULL, NULL),
  ('net_income', '净利润', '["净利润", "五、净利润"]', '["净利润率", "净利润增长率", "净利润预测", "归属于母公司"]', 'income', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 33, '本报告期实现净利润【数值】元。', 'synthetic_example', NULL, '含少数股东损益。排除词必须挡住「归属于母公司」，那是子项不是同一指标'),
  ('net_income_parent', '归属于母公司股东的净利润', '["归属于母公司所有者的净利润", "归属于母公司股东的净利润", "归属于上市公司股东的净利润"]', '["归母净利润率", "归母净利润增长率", "归母净利润预测"]', 'income', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, 'net_income', 34, '归属于母公司股东的净利润为【数值】元。', 'synthetic_example', NULL, '「其中：」层级子项'),
  ('minority_interest_pl', '少数股东损益', '["少数股东损益"]', NULL, 'income', 'flow', 'currency', 'neutral', 0, 0, NULL, 'net_income', 35, NULL, NULL, NULL, NULL),
  ('non_gaap_net_income', '扣除非经常性损益后的净利润', '["扣除非经常性损益后的净利润", "归属于上市公司股东的扣除非经常性损益的净利润", "扣非净利润"]', '["非经常性损益", "扣非净利润率", "扣非净利润增长率"]', 'income', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 36, '扣除非经常性损益后的净利润为【数值】元。', 'synthetic_example', NULL, '与 net_income 对照使用'),
  ('non_recurring_gain_loss', '非经常性损益', '["非经常性损益", "非经常性损益合计"]', '["扣非净利润", "扣除非经常性损益后的净利润"]', 'income', 'flow', 'currency', 'neutral', 1, 0, NULL, NULL, 37, '归属于上市公司股东的非经常性损益为【数值】元。', 'synthetic_example', NULL, '★ 须单独列示，是扣非净利润对照的基础'),
  ('government_grant', '政府补助', '["政府补助", "计入当期损益的政府补助"]', '["政府补助递延收益", "政府补助退回", "补助收入确认"]', 'income', 'flow', 'currency', 'neutral', 1, 0, NULL, 'non_recurring_gain_loss', 38, '本年度计入当期损益的政府补助为【数值】元。', 'synthetic_example', NULL, '钢铁企业金额常较大，单列。持续性经营补贴可保留，一次性补助剔除'),
  ('asset_disposal_gain_loss', '资产处置收益', '["资产处置收益", "资产处置损益"]', '["固定资产处置", "资产处置收益率", "营业外收入"]', 'income', 'flow', 'currency', 'neutral', 1, 0, NULL, 'non_recurring_gain_loss', 39, '本年度资产处置收益为【数值】元。', 'synthetic_example', NULL, '非日常处置剔除'),
  ('impairment_loss', '资产减值损失', '["资产减值损失"]', '["信用减值损失", "商誉减值", "资产减值准备"]', 'income', 'flow', 'currency', 'negative_is_good', 1, 0, NULL, NULL, 40, '本年度确认资产减值损失【数值】元。', 'synthetic_example', NULL, '⚠ 与 credit_impairment_loss 是利润表上并列的两行，合并会重复计算'),
  ('credit_impairment_loss', '信用减值损失', '["信用减值损失"]', '["资产减值损失", "坏账准备", "预期信用损失"]', 'income', 'flow', 'currency', 'negative_is_good', 1, 0, NULL, NULL, 41, '本年度确认信用减值损失【数值】元。', 'synthetic_example', NULL, '新金融工具准则下的独立行项，2019 年起与资产减值损失分列'),
  ('goodwill_impairment', '商誉减值损失', '["商誉减值损失"]', '["商誉账面价值", "资产减值损失"]', 'income', 'flow', 'currency', 'negative_is_good', 1, 0, NULL, 'impairment_loss', 42, '本年度计提商誉减值损失【数值】元。', 'synthetic_example', NULL, '资产减值损失下的明细项；不机械剔除，逐年判断'),
  ('fair_value_gain_loss', '公允价值变动收益', '["公允价值变动收益", "公允价值变动损益"]', '["其他综合收益", "交易性金融资产", "公允价值计量"]', 'income', 'flow', 'currency', 'neutral', 1, 0, NULL, NULL, 43, '本年度公允价值变动收益为【数值】元。', 'synthetic_example', NULL, '非主营金融资产收益通常剔除'),
  ('investment_income', '投资收益', '["投资收益"]', '["投资活动现金流", "长期股权投资", "其他收益"]', 'income', 'flow', 'currency', 'neutral', 1, 0, NULL, NULL, 44, '本年度投资收益为【数值】元。', 'synthetic_example', NULL, '与主营高度相关且持续发生的联营/合营收益可保留，但需单独标记'),
  -- ---- 资产负债表
  ('total_assets', '资产总计', '["资产总计", "资产总额"]', '["总资产周转率", "资产负债率", "资产总额增长率"]', 'balance', 'stock', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 100, '报告期末资产总额为【数值】元。', 'synthetic_example', NULL, NULL),
  ('total_liabilities', '负债合计', '["负债合计", "负债总额"]', '["资产负债率", "有息负债", "流动负债合计"]', 'balance', 'stock', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 101, '报告期末负债总额为【数值】元。', 'synthetic_example', NULL, NULL),
  ('total_equity', '所有者权益合计', '["所有者权益合计", "股东权益合计", "所有者权益(或股东权益)合计"]', '["归属于母公司"]', 'balance', 'stock', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 102, NULL, NULL, NULL, '含少数股东权益'),
  ('equity_parent', '归属于母公司所有者权益合计', '["归属于母公司所有者权益合计", "归属于母公司股东权益合计", "归属于上市公司股东的所有者权益"]', '["少数股东权益", "股东权益合计", "净资产收益率"]', 'balance', 'stock', 'currency', 'positive_is_good', 0, 0, NULL, 'total_equity', 103, '报告期末归属于母公司股东的权益为【数值】元。', 'synthetic_example', NULL, '排除词必须挡住「股东权益合计」，那是父项'),
  ('minority_interest', '少数股东权益', '["少数股东权益"]', NULL, 'balance', 'stock', 'currency', 'neutral', 0, 0, NULL, 'total_equity', 104, NULL, NULL, NULL, NULL),
  ('cash_and_equivalents', '货币资金', '["货币资金", "库存现金及现金等价物"]', '["现金净增加额", "现金流量净额"]', 'balance', 'stock', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 105, NULL, NULL, NULL, '时点数。与 cash_end（现金流量表的期末余额）不是同一口径，后者剔除了受限资金'),
  ('accounts_receivable', '应收账款', '["应收账款"]', '["应收账款周转率", "应收票据及应收账款", "坏账准备"]', 'balance', 'stock', 'currency', 'neutral', 0, 0, NULL, NULL, 106, '期末应收账款账面余额为【数值】元。', 'synthetic_example', NULL, '⚠ 必须排除「应收票据及应收账款」，那是含本科目在内的合并列示行，两边都收会重复计算'),
  ('notes_and_ar', '应收票据及应收账款', '["应收票据及应收账款"]', '["应收账款", "应收票据", "应收账款周转率"]', 'balance', 'stock', 'currency', 'neutral', 0, 0, NULL, NULL, 107, '期末应收票据及应收账款为【数值】元。', 'synthetic_example', NULL, '2018 年起年报的合并列示行 = accounts_receivable + notes_receivable，三者只能用其一'),
  ('notes_receivable', '应收票据', '["应收票据"]', '["应收票据及应收账款", "应收款项融资"]', 'balance', 'stock', 'currency', 'neutral', 0, 0, NULL, NULL, 108, NULL, NULL, NULL, '若公司按合并列示行披露，则本指标无值，应由 notes_and_ar 承载。应收款项融资是单独列示的票据，口径不同'),
  ('inventory', '存货', '["存货"]', '["存货周转率", "存货跌价准备", "存货占比"]', 'balance', 'stock', 'currency', 'neutral', 0, 0, NULL, NULL, 109, '期末存货账面余额为【数值】元。', 'synthetic_example', NULL, '钢铁企业需区分原材料/在产品/产成品'),
  ('contract_liabilities', '合同负债', '["合同负债", "预收款项"]', '["合同资产", "合同负债率", "负债合同"]', 'balance', 'stock', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 110, '期末合同负债为【数值】元。', 'synthetic_example', NULL, '原「预收款项」，2020 年起按新收入准则列示为合同负债'),
  ('accounts_payable', '应付账款', '["应付账款"]', '["应付票据及应付账款", "应付账款周转率"]', 'balance', 'stock', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 111, '期末应付账款为【数值】元。', 'synthetic_example', NULL, '同 accounts_receivable，须排除合并列示行'),
  ('ppe', '固定资产', '["固定资产"]', '["固定资产周转率", "固定资产原值", "固定资产减值准备"]', 'balance', 'stock', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 112, '期末固定资产账面价值为【数值】元。', 'synthetic_example', NULL, '取账面价值（净额），不是原值'),
  ('cip', '在建工程', '["在建工程"]', '["工程物资"]', 'balance', 'stock', 'currency', 'neutral', 0, 0, NULL, NULL, 113, NULL, NULL, NULL, '重资产行业资本开支进度的观察项'),
  ('goodwill', '商誉', '["商誉"]', '["商誉减值准备", "商誉占比", "商誉评估"]', 'balance', 'stock', 'currency', 'neutral', 0, 0, NULL, NULL, 114, '期末商誉账面价值为【数值】元。', 'synthetic_example', NULL, NULL),
  ('short_term_borrowing', '短期借款', '["短期借款"]', NULL, 'balance', 'stock', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 115, NULL, NULL, NULL, NULL),
  ('long_term_borrowing', '长期借款', '["长期借款"]', NULL, 'balance', 'stock', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 116, NULL, NULL, NULL, NULL),
  ('bonds_payable', '应付债券', '["应付债券"]', NULL, 'balance', 'stock', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 117, NULL, NULL, NULL, NULL),
  ('lease_liability', '租赁负债', '["租赁负债"]', NULL, 'balance', 'stock', 'currency', 'negative_is_good', 0, 0, NULL, NULL, 118, NULL, NULL, NULL, NULL),
  ('interest_bearing_debt', '有息负债合计', '[]', NULL, 'balance', 'stock', 'currency', 'negative_is_good', 0, 1, NULL, NULL, 119, NULL, NULL, NULL, '计算值 = 短期借款 + 长期借款 + 应付债券 + 租赁负债 + 一年内到期的非流动负债'),
  ('net_debt', '净债务', '[]', NULL, 'balance', 'stock', 'currency', 'negative_is_good', 0, 1, NULL, NULL, 120, NULL, NULL, NULL, '计算值 = 有息负债 − 非经营性现金'),
  -- ---- 现金流量表
  ('cfo', '经营活动产生的现金流量净额', '["经营活动产生的现金流量净额", "经营活动现金流量净额"]', '["投资活动产生的现金流量净额", "筹资活动产生的现金流量净额", "经营现金流率"]', 'cashflow', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 200, '经营活动产生的现金流量净额为【数值】元。', 'synthetic_example', NULL, '★ 三个活动现金流互为排除词，是最容易被误映射的一组'),
  ('cfi', '投资活动产生的现金流量净额', '["投资活动产生的现金流量净额"]', '["经营活动产生的现金流量净额", "筹资活动产生的现金流量净额"]', 'cashflow', 'flow', 'currency', 'neutral', 0, 0, NULL, NULL, 201, '投资活动产生的现金流量净额为【数值】元。', 'synthetic_example', NULL, NULL),
  ('cff', '筹资活动产生的现金流量净额', '["筹资活动产生的现金流量净额"]', '["经营活动产生的现金流量净额", "投资活动产生的现金流量净额"]', 'cashflow', 'flow', 'currency', 'neutral', 0, 0, NULL, NULL, 202, '筹资活动产生的现金流量净额为【数值】元。', 'synthetic_example', NULL, NULL),
  ('depreciation_amortization', '固定资产折旧、油气资产折耗、生产性生物资产折旧', '["固定资产折旧", "折旧与摊销", "固定资产折旧、油气资产折耗、生产性生物资产折旧"]', '["累计折旧"]', 'cashflow', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 203, NULL, NULL, NULL, 'FCFF 计算必需。取现金流量表补充资料的本期计提数，不是资产负债表累计折旧'),
  ('amortization_intangible', '无形资产摊销', '["无形资产摊销", "使用权资产折旧"]', '["累计摊销"]', 'cashflow', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 204, NULL, NULL, NULL, NULL),
  ('capex', '购建固定资产、无形资产和其他长期资产支付的现金', '["购建固定资产、无形资产和其他长期资产支付的现金", "购建固定资产、无形资产和其他长期资产所支付的现金"]', '["固定资产原值", "固定资产账面价值", "资本化研发支出"]', 'cashflow', 'flow', 'currency', 'neutral', 0, 0, NULL, NULL, 205, '本年度购建固定资产、无形资产和其他长期资产支付的现金为【数值】元。', 'synthetic_example', NULL, '资本开支，FCFF 计算必需'),
  ('cash_begin', '期初现金及现金等价物余额', '["期初现金及现金等价物余额"]', '["期末现金及现金等价物余额"]', 'cashflow', 'stock', 'currency', 'neutral', 0, 0, NULL, NULL, 206, NULL, NULL, NULL, NULL),
  ('cash_end', '期末现金及现金等价物余额', '["期末现金及现金等价物余额"]', '["现金净增加额", "现金流量净额", "货币资金"]', 'cashflow', 'stock', 'currency', 'neutral', 0, 0, NULL, NULL, 207, '期末现金及现金等价物余额为【数值】元。', 'synthetic_example', NULL, NULL),
  ('cash_net_increase', '现金及现金等价物净增加额', '["现金及现金等价物净增加额"]', '["期末现金及现金等价物余额", "经营现金流净额"]', 'cashflow', 'flow', 'currency', 'neutral', 0, 0, NULL, NULL, 208, '现金及现金等价物净增加额为【数值】元。', 'synthetic_example', NULL, '应等于 cfo + cfi + cff，是三表勾稽的校验点'),
  -- ---- 资产负债表
  ('operating_working_capital', '经营性营运资本', '[]', NULL, 'balance', 'stock', 'currency', 'neutral', 0, 1, NULL, NULL, 209, NULL, NULL, NULL, '计算值 = (应收账款 + 应收票据 + 存货) − (应付账款 + 应付票据 + 合同负债)'),
  ('diluted_shares', '稀释后总股本', '["稀释后总股本"]', '["总股本", "基本每股收益对应的股本"]', 'balance', 'stock', 'shares', 'neutral', 0, 0, NULL, NULL, 300, NULL, NULL, NULL, 'EV→股权价值 必需。与「总股本」不是一回事，可转债/期权会稀释'),
  -- ---- 股本与每股
  ('eps_basic', '基本每股收益', '["基本每股收益"]', '["稀释每股收益", "基本每股收益率", "每股收益增长率"]', 'indicator', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 301, '本年度基本每股收益为【数值】元/股。', 'synthetic_example', NULL, '单位是元/股，不是股数'),
  ('eps_diluted', '稀释每股收益', '["稀释每股收益"]', '["基本每股收益", "稀释每股收益率"]', 'indicator', 'flow', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 302, '本年度稀释每股收益为【数值】元/股。', 'synthetic_example', NULL, NULL),
  ('bvps', '每股净资产', '["归属于上市公司股东的每股净资产", "每股净资产"]', NULL, 'indicator', 'stock', 'currency', 'positive_is_good', 0, 0, NULL, NULL, 303, NULL, NULL, NULL, NULL),
  -- ---- 利润表
  ('ebit', '息税前利润', '[]', NULL, 'income', 'flow', 'currency', 'positive_is_good', 0, 1, NULL, NULL, 400, NULL, NULL, NULL, '计算值 = 利润总额 + 利息费用 − 利息收入；★ 正常化核心指标的分母基础'),
  ('ebitda', '息税折旧摊销前利润', '[]', NULL, 'income', 'flow', 'currency', 'positive_is_good', 0, 1, NULL, NULL, 401, NULL, NULL, NULL, '计算值 = EBIT + 折旧摊销'),
  -- ---- 股本与每股
  ('ebit_margin', 'EBIT 利润率', '[]', NULL, 'indicator', 'ratio', 'percent', 'positive_is_good', 0, 1, NULL, NULL, 402, NULL, NULL, NULL, '★ 周期正常化的核心指标'),
  ('ebitda_margin', 'EBITDA 利润率', '[]', NULL, 'indicator', 'ratio', 'percent', 'positive_is_good', 0, 1, NULL, NULL, 403, NULL, NULL, NULL, '交叉验证项'),
  ('gross_margin', '毛利率', '[]', NULL, 'indicator', 'ratio', 'percent', 'positive_is_good', 0, 1, NULL, NULL, 404, NULL, NULL, NULL, NULL),
  ('net_margin', '净利率', '[]', NULL, 'indicator', 'ratio', 'percent', 'positive_is_good', 0, 1, NULL, NULL, 405, NULL, NULL, NULL, NULL),
  ('roe', '净资产收益率', '[]', NULL, 'indicator', 'ratio', 'percent', 'positive_is_good', 0, 1, NULL, NULL, 406, NULL, NULL, NULL, '平均净资产口径'),
  ('roic', '投入资本回报率', '[]', NULL, 'indicator', 'ratio', 'percent', 'positive_is_good', 0, 1, NULL, NULL, 407, NULL, NULL, NULL, '交叉验证项 = NOPAT / (有息负债 + 股东权益)'),
  ('cash_conversion', '现金转化率', '[]', NULL, 'indicator', 'ratio', 'percent', 'positive_is_good', 0, 1, NULL, NULL, 408, NULL, NULL, NULL, '计算值 = 经营现金流 / 净利润'),
  ('nonrecurring_share', '非经常性损益占比', '[]', NULL, 'indicator', 'ratio', 'percent', 'negative_is_good', 0, 1, NULL, NULL, 409, NULL, NULL, NULL, NULL),
  -- ---- 钢铁专属
  ('steel_output', '钢材产量', '["钢材产量"]', '["粗钢产量", "生铁产量", "钢材销量", "钢材产能", "钢材销售收入"]', 'industry', 'flow', 'ton', 'positive_is_good', 0, 0, 'steel', NULL, 500, '本年度钢材产量为【数值】万吨。', 'synthetic_example', NULL, '年报「经营情况讨论与分析」产销量表。产量与销量是两张表，互为排除词'),
  ('crude_steel_output', '粗钢产量', '["粗钢产量"]', '["钢材产量", "钢材销量", "粗钢产能"]', 'industry', 'flow', 'ton', 'positive_is_good', 0, 0, 'steel', NULL, 501, '本年度粗钢产量为【数值】万吨。', 'synthetic_example', NULL, '⚠ 粗钢产量 ≠ 钢材产量，两者相差一个成材率。产能利用率的分母必须与之配套'),
  ('steel_sales_volume', '钢材销量', '["钢材销量", "销售量"]', '["粗钢产量", "钢材产量", "钢材产能", "钢材销售收入"]', 'industry', 'flow', 'ton', 'positive_is_good', 0, 0, 'steel', NULL, 502, '本年度钢材销量为【数值】万吨。', 'synthetic_example', NULL, '吨钢指标的分母用销量。别名「销售量」与煤企共用，映射时须先按 project.industry 过滤字段'),
  ('steel_price_avg', '钢材平均售价', '[]', NULL, 'industry', 'ratio', 'currency', 'positive_is_good', 0, 1, 'steel', NULL, 503, NULL, NULL, NULL, '计算值 = 钢材销售收入 / 钢材销量'),
  ('steel_spread', '吨钢原料差价', '["吨钢原料差价"]', '["吨钢毛利", "吨钢 EBITDA", "吨钢成本"]', 'industry', 'ratio', 'currency', 'positive_is_good', 0, 1, 'steel', NULL, 504, '本年度吨钢原料差价为【数值】元/吨。', 'synthetic_example', NULL, '⚠ 若只能得到「售价 − 原材料成本」，就只能叫原料差价。称为毛利会虚高约一倍——差的是一人工、制造费用与折旧'),
  ('steel_gross_profit_per_ton', '吨钢毛利', '["吨钢毛利"]', '["吨钢原料差价", "吨钢 EBITDA", "吨钢成本"]', 'industry', 'ratio', 'currency', 'positive_is_good', 0, 1, 'steel', NULL, 505, '本年度吨钢毛利为【数值】元/吨。', 'synthetic_example', NULL, '优先采用年报直接披露值；自算时分母用销量，成本须含材料/人工/制造费用/折旧'),
  ('steel_ebitda_per_ton', '吨钢 EBITDA', '["吨钢 EBITDA"]', '["吨钢毛利", "吨钢原料差价", "EBITDA 利润率"]', 'industry', 'ratio', 'currency', 'positive_is_good', 0, 1, 'steel', NULL, 506, '本年度吨钢 EBITDA 为【数值】元/吨。', 'synthetic_example', NULL, '交叉验证项，一律不影响 DCF'),
  ('effective_capacity', '有效产能', '["有效产能", "钢材有效产能", "粗钢有效产能"]', '["设计产能", "产能利用率", "产能置换"]', 'industry', 'stock', 'ton', 'neutral', 0, 0, 'steel', NULL, 507, '报告期钢材有效产能为【数值】万吨。', 'synthetic_example', NULL, '产能利用率的默认分母。优先取公司披露的有效产能，不是设计产能'),
  ('capacity_utilization', '产能利用率', '["产能利用率"]', '["设计产能", "有效产能", "产能利用率目标"]', 'industry', 'ratio', 'percent', 'positive_is_good', 0, 1, 'steel', NULL, 508, '本年度粗钢产能利用率为【数值】%。', 'synthetic_example', NULL, '交叉验证项。一体化钢企用粗钢口径，轧钢企业用钢材口径，两者不得混用'),
  ('crude_steel_capacity', '粗钢产能', '["粗钢产能", "钢铁产能"]', '["设计产能", "有效产能"]', 'industry', 'stock', 'ton', 'neutral', 0, 0, 'steel', NULL, 509, NULL, NULL, NULL, '设计产能不是本字段；按 docs/03 产能利用率默认取有效产能'),
  -- ---- 能源专属
  ('coal_output', '原煤产量', '["原煤产量", "商品煤产量", "煤炭产量"]', '["煤炭销量", "原煤销量"]', 'industry', 'flow', 'ton', 'positive_is_good', 0, 0, 'energy', NULL, 520, NULL, NULL, NULL, NULL),
  ('coal_sales_volume', '商品煤销量', '["商品煤销量", "煤炭销量", "销售量"]', '["原煤产量", "商品煤产量"]', 'industry', 'flow', 'ton', 'positive_is_good', 0, 0, 'energy', NULL, 521, NULL, NULL, NULL, '别名「销售量」与钢企共用，映射时须先按 project.industry 过滤字段'),
  ('coal_price_avg', '吨煤平均售价', '["吨煤售价", "煤炭平均售价"]', NULL, 'industry', 'ratio', 'currency', 'positive_is_good', 0, 1, 'energy', NULL, 522, NULL, NULL, NULL, NULL),
  ('ton_coal_gross_margin', '吨煤毛利', '["吨煤毛利"]', '["吨煤原料差价", "吨煤成本"]', 'industry', 'ratio', 'currency', 'positive_is_good', 0, 1, 'energy', NULL, 523, NULL, NULL, NULL, '交叉验证项。煤企是钢企的成本项，两者周期驱动因素相反，不得互为估值可比'),
  ('safety_production_expense', '安全生产费用', '["安全生产费用", "专项储备"]', NULL, 'industry', 'flow', 'currency', 'neutral', 0, 0, 'energy', NULL, 524, NULL, NULL, NULL, '能源钢铁行业特有，须单列'),
  -- ---- 其他
  ('environmental_protection_expense', '环保投入', '["环保投入", "环保支出", "环保费用"]', NULL, 'industry', 'flow', 'currency', 'neutral', 0, 0, NULL, NULL, 525, NULL, NULL, NULL, '双碳/能耗双控相关'),
  -- ---- 披露事项（无数值）
  ('audit_opinion', '审计意见类型', '["审计意见", "审计意见类型"]', '["内部控制审计意见", "审计费用", "审计委员会"]', 'disclosure', 'text', 'text', 'positive_is_good', 0, 0, NULL, NULL, 600, '会计师事务所对公司本年度财务报告出具了【审计意见类型】。', 'synthetic_example', NULL, '非标意见是最高优先级风险信号。文本型，不入 financial_fact'),
  ('accounting_policy_change', '会计政策变更', '["会计政策变更"]', '["会计估计变更", "前期差错更正", "会计政策和会计估计"]', 'disclosure', 'text', 'text', 'neutral', 0, 0, NULL, NULL, 601, '本年度因会计政策变更对比较数据进行追溯调整。', 'synthetic_example', NULL, '触发 incomparable_reason = policy_change。会计估计变更与前期差错更正是另两件事，排除但不另立指标。文本型，不入 financial_fact');
-- END GENERATED: metric_definitions
--
-- CSV 往返工具会把每个字段的六项审校内容摊平成表格里的列：
--   标准名称 label_cn｜别名 aliases｜适用报表 statement｜排除词 exclusion_terms
--   ｜合并母公司口径 scope_note｜例句 example_sentence + example_source
-- 多值列用 | 分隔（导入时也接受分号）。
