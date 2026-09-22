-- =============================================================================
-- 旧键名迁移映射（签字文档 accounting_signoff_v1 §A-1 与 §A-3）
--
-- 「旧键名只保留在迁移映射表中，不再写入新数据。」
--
-- 为什么值得单建一张表：改名这件事本身**不会报错**。队友按早先的设计文档
-- （大框架-投研程序设计初版.docx）写出 'net_profit'，或者从旧分支复制一段
-- 写死 'fixed_assets' 的代码，运行时只会得到「这个字段没有数据」——
-- 查不出数据看起来和「公司没披露这一项」一模一样。
--
-- 有了这张表，仓储层与解析层就能把这类错误报成
--   「'net_profit' 已改名为 'net_income'（依据 accounting_signoff_v1 A-1）」
-- 而不是让它静默变成一处缺失。
--
-- ⚠ 两个外键（legacy_key 是主键、metric_key 指向 metric_definition）意味着
--   旧键名既写不进 financial_fact，也不会指向一个不存在的字段。
-- =============================================================================

INSERT INTO metric_key_migration (legacy_key, metric_key, decided_by, note) VALUES
-- ---- §A-1 字段键名（13 项）----
('total_profit',            'profit_before_tax',        'accounting_signoff_v1 A-1', '总括口径改称税前口径，与利润表「利润总额」行对应'),
('net_profit',              'net_income',               'accounting_signoff_v1 A-1', ''),
('net_profit_attr_parent',  'net_income_parent',        'accounting_signoff_v1 A-1', 'attr_parent 缩写不统一，改为 parent'),
('deducted_net_profit',     'non_gaap_net_income',      'accounting_signoff_v1 A-1', '「扣非」是俗称，正式名为非经常性损益扣除后净利润'),
('financial_expense',       'finance_expense',          'accounting_signoff_v1 A-1', '与利润表「财务费用」行名一致'),
('equity_attr_parent',      'equity_parent',            'accounting_signoff_v1 A-1', ''),
('contract_liability',      'contract_liabilities',     'accounting_signoff_v1 A-1', '资产负债表上为复数口径'),
('fixed_assets',            'ppe',                      'accounting_signoff_v1 A-1', '用国际通行缩写，避免与「固定资产清理」等相邻行混淆'),
('asset_impairment',        'impairment_loss',          'accounting_signoff_v1 A-1', '与利润表「资产减值损失」行名一致'),
('nonrecurring_pl',         'non_recurring_gain_loss',  'accounting_signoff_v1 A-1', 'pl 后辍不表意，改为完整词'),
('gov_subsidy',             'government_grant',         'accounting_signoff_v1 A-1', '与「政府补助」准则用词一致'),
('asset_disposal_gain',     'asset_disposal_gain_loss', 'accounting_signoff_v1 A-1', '处置可能为收益也可能为损失，键名必须双向'),
('ton_steel_gross_margin',  'steel_gross_profit_per_ton','accounting_signoff_v1 A-1', '吨钢毛利是每吨口径的金额，不是率'),

-- ---- §A-3 应收票据与应收账款（跨年度主字段）----
-- 「trade_receivables = notes_and_ar」：2018 年起年报以「应收票据及应收账款」合并列示，
-- 2018 年之前拆成「应收票据」「应收账款」两行。跨年度做应收周转时必须用同一个键，
-- 否则 2017→2018 会出现一次纯粹由列示方式变化造成的假跳变。
-- 拆分项（accounts_receivable / notes_receivable）仍可入库作明细，
-- 但**不得**与 notes_and_ar 同时计入营运资本——两边都收会重复计算。
('trade_receivables',       'notes_and_ar',             'accounting_signoff_v1 A-3', '跨年度主字段；与拆分项不得重复计入营运资本');
