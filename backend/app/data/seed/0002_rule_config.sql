-- =============================================================================
-- 规则参数初值
--
-- 这些是 docs/03-valuation-rules.md 与 docs/04-index-rules.md 的机读版本。
-- 页面提供「查看 / 修改 / 恢复默认」入口，每次变更写入 rule_config_version，
-- 并记入 run_manifest.rule_config_version，保证「同一输入可复现同一结果」。
--
-- industry = '' 表示全局默认；填具体行业（steel / energy）则只覆盖该行业。
-- 新增行业只需加一组行，**不需要改代码**。
--
-- tier 列（签字文档 A-8）区分三类参数，决定改它需要谁点头：
--   hard（默认）字段口径、EBIT、周期窗口、阈值、减值、估值公式 —— 发布前必须会计签字
--   soft        提示语、颜色、排序 —— 可后续调整
--   模型参数     模型名、Prompt 版本、温度、输出长度 —— 需记录版本，不属于会计口径
-- 不写 tier 的行一律按 hard 处理。**这个默认方向是刻意的**：让参数「不算数」
-- 需要有人明确声明，而不是靠忘记写 tier 来偷偷降级。
-- =============================================================================

-- ------------------------------------------------- 周期正常化（能源钢铁核心口径）
INSERT INTO rule_config
  (key, value, value_type, industry, label_cn, description, unit, default_value, min_value, max_value)
VALUES
('normalization.preferred_years', '8', 'integer', '',
 '主窗口年数',
 '主正常化窗口的完整财务年度数。窗口锚定在最新完整年度上滚动，不人为挑选区间。',
 '年', '8', '8', '8'),

('normalization.fallback_years', '10', 'integer', '',
 '回退窗口年数',
 '主窗口可比年度不足时扩展到此年数重试。',
 '年', '10', '10', '10'),

('normalization.min_comparable_years', '7', 'integer', '',
 '主窗口最少可比年度',
 '8 年窗口内至少保留 7 个可比年度（即最多剔除 1 个）。不足则自动切换至 10 年窗口。',
 '年', '7', '5', '8'),

('normalization.min_comparable_years_fallback', '8', 'integer', '',
 '回退窗口最少可比年度',
 '10 年窗口内至少保留 8 个可比年度。仍不足则返回 NORMALIZATION_INSUFFICIENT_DATA。',
 '年', '8', '6', '10'),

-- ★ 高低盈利阶段的判定门槛（签字文档 A-6：高低盈利阶段最低落差 2 个百分点）。
--
-- 用「相对中枢的绝对落差」判定，**不用分位数**。
-- 分位数判定对本参数是有害的：任何窗口都能找出「最高的那个」和「最低的那个」年份，
-- 条件恒为真，校验拦不住任何东西——一个全部由平淡年份组成的窗口照样能通过，
-- 然后这套平淡的 margin 会被当成「穿越周期的中枢」去做永续增长。
-- 绝对落差则能拦住它：所有年份都挤在中枢附近时，两个条件同时不成立。
-- 对应的实现是 app/engine/normalization.py::check_cycle_coverage。
('normalization.phase_min_spread', '0.02', 'percent', '',
 '高低盈利阶段最低落差',
 'EBIT margin 需相对中枢上下各偏离该幅度，才算覆盖到高盈利/低盈利阶段。',
 '比例', '0.02', '0.005', '0.10'),

('normalization.ebit_formula', '利润总额 + 利息费用 − 利息收入', 'enum', '',
 'Reported EBIT 公式',
 '主 DCF 的默认口径。历史轻解析阶段即按此式计算。',
 '', '利润总额 + 利息费用 − 利息收入', '', ''),

('normalization.ebit_crosscheck_formula', '营业利润 + 财务费用', 'enum', '',
 'Cross-check EBIT 公式',
 '当两份年报数据齐备时用于交叉核对。两法差异超过容差时进入 needs_review。',
 '', '营业利润 + 财务费用', '', ''),

('normalization.ebit_crosscheck_tolerance', '0.05', 'percent', '',
 'EBIT 两法差异容差',
 '两种算法差异超过该比例时进入人工复核。',
 '比例', '0.05', '0.01', '0.20'),

('normalization.default_ebit_variant', 'reported', 'enum', '',
 'DCF 默认 EBIT 口径',
 '会计同学批准调整之前一律使用 reported；批准后才切到 adjusted。',
 '', 'reported', '', ''),

('normalization.impairment_policy', 'case_by_case', 'enum', '',
 '资产减值处理',
 '不机械剔除。反映产能过剩、资产竞争力下降或长期盈利恶化的减值不加回；有充分证据证明是一次性、非重复且不代表持续经营能力下降的，可以加回。',
 '', 'case_by_case', '', ''),

('normalization.impairment_addback_requires_evidence', '1', 'bool', '',
 '减值加回需证据',
 '置 1 时，任何减值加回都必须通过 ebit_adjustment 留下理由与来源页码，并经会计复核。',
 '', '1', '0', '1'),

('normalization.core_metric', 'ebit_margin', 'enum', '',
 '正常化核心指标',
 '收入加权周期中位 EBIT margin。这是 DCF 的唯一起点。',
 '', 'ebit_margin', '', ''),

('normalization.weighting', 'revenue_weighted', 'enum', '',
 '加权方式',
 '按各年营业收入加权，而非简单算术平均——避免小年度的异常利润率被过度放大。',
 '', 'revenue_weighted', '', ''),

('normalization.require_full_cycle', '1', 'bool', '',
 '必须通过周期覆盖校验',
 '置 1 时，未同时覆盖高盈利与低盈利阶段即判定 incomplete_cycle，禁止正常化。',
 '', '1', '0', '1'),

('normalization.allow_forced', '0', 'bool', '',
 '允许强行正常化',
 '固定为 0。数据不足时系统必须拒绝计算，不得退回未经校验的假设值。',
 '', '0', '0', '0'),

('normalization.require_accounting_review', '1', 'bool', '',
 '可比年度须经会计复核',
 '置 1 时，剔除不可比年度必须先由会计同学确认，系统不自动判定。',
 '', '1', '0', '1'),

-- ---- 交叉验证各项的口径（一律不影响 DCF）----
('normalization.crosscheck_metrics',
 '["ton_steel_gross_margin","ebitda_margin","capacity_utilization","roic"]',
 'enum', '',
 '交叉验证指标',
 '仅作参考，不参与核心计算。用于回答「EBIT margin 中枢这个结论稳不稳」。',
 '',
 '["ton_steel_gross_margin","ebitda_margin","capacity_utilization","roic"]', '', ''),

('normalization.crosscheck_agreement_tolerance', '0.20', 'percent', '',
 '交叉验证一致容差',
 '交叉验证指标推算的中枢与核心 EBIT margin 中枢相差超过该比例时，标记为需要人工复核。',
 '比例', '0.20', '0.05', '0.50'),

('crosscheck.ton_steel_gross_margin.denominator', 'sales_volume', 'enum', '',
 '吨钢毛利分母',
 '用销售量。若只能得到「售价 − 原材料成本」，必须命名为吨钢原料差价，不得称为吨钢毛利。',
 '', 'sales_volume', '', ''),

('crosscheck.capacity_utilization.denominator', 'output', 'enum', '',
 '产能利用率分母',
 '用产量（与吨钢毛利相反）。一体化钢企用粗钢，轧钢企业用钢材，两者不得混用。',
 '', 'output', '', ''),

('crosscheck.capacity_utilization.capacity_basis', 'effective', 'enum', '',
 '产能口径',
 '采用公司披露的有效产能利用率，不以设计产能作为默认口径。',
 '', 'effective', '', ''),

('crosscheck.roic.capital_basis', 'average', 'enum', '',
 'ROIC 投入资本口径',
 '取期初期末平均值，不用单一时点值。',
 '', 'average', '', ''),

('crosscheck.roic.goodwill', 'included', 'enum', '',
 'ROIC 商誉处理',
 '主口径保留商誉，因为商誉是企业实际投入资本的一部分；扣商誉口径只作敏感性分析，两者不得混用。',
 '', 'included', '', ''),

-- ------------------------------------------------- 估值
--
-- ⚠ `description` 里的 `⬜ 待会计确认` 前缀是**机器可读的标记**：
--   它表示这个参数「已经实现、正在生效，但没人签字过」。
--   清单在仓库根目录的 `待会计确认.md`，两边由
--   `tests/unit/db/test_pending_accounting.py` 盯着，分叉会测试失败。
--   去掉这个前缀 = 声明它已经过会计确认，**不是顺手清理**。
('valuation.terminal_growth_max', '0.02', 'percent', '',
 '终值增长率上限',
 '⬜ 待会计确认（A-6）｜周期行业不得给高永续增速，上限取长期通胀水平。方向有共识，但 0.02 这个具体数字是实现时拍的。',
 '比例', '0.02', '0.00', '0.03'),

('valuation.wacc_min', '0.06', 'percent', '',
 'WACC 下限',
 '⬜ 待会计确认（A-6）｜防止压力情景下 WACC 被推到不切实际的水平。方向有共识，但 0.06 这个具体数字是实现时拍的。',
 '比例', '0.06', '0.03', '0.15'),

('valuation.require_exit_multiple_check', '1', 'bool', '',
 '强制退出倍数校验',
 '永续增长法与退出倍数法结果须并列展示；两法对不齐时必须说明原因。',
 '', '1', '0', '1'),

-- ------------------------------------------------- 会计校验
('check.balance_tolerance', '0.005', 'percent', '',
 '三表勾稽相对容差',
 '资产 = 负债 + 所有者权益 等勾稽项的允许相对偏差。超出则优先怀疑解析错误，把相关事实降级为 needs_review。',
 '比例', '0.005', '0.001', '0.02'),

('check.cash_conversion_warn', '1.0', 'percent', '',
 '现金转化率预警线',
 '经营活动现金流 / 净利润 低于该值时提示关注回款质量。',
 '倍', '1.0', '0.5', '2.0'),

('check.receivable_growth_gap_warn', '0.20', 'percent', '',
 '应收账款增速差预警线',
 '应收账款增速 − 收入增速 超过该值时提示收入质量风险。',
 '比例', '0.20', '0.10', '0.50');

-- ------------------------------------------------- 非经常性损益分类处理（方案选择第 9 条）
-- split = 需逐年判断是一次性还是持续性；case_by_case = 不机械剔除，逐笔判断
INSERT INTO rule_config
  (key, value, value_type, industry, label_cn, description, unit, default_value, min_value, max_value)
VALUES
('nonrecurring.gov_subsidy', 'split', 'enum', '', '政府补助',
 '一次性补助剔除；持续性经营补贴可保留。钢铁企业此项金额常较大，须逐年判断。', '', 'split', '', ''),
('nonrecurring.asset_disposal', 'exclude', 'enum', '', '资产处置收益/损失',
 '非日常处置剔除。', '', 'exclude', '', ''),
('nonrecurring.investment_income', 'split', 'enum', '', '投资收益',
 '非主营投资收益剔除；与主营业务高度相关且持续发生的联营/合营收益可保留，但需单独标记。', '', 'split', '', ''),
('nonrecurring.fair_value_change', 'exclude', 'enum', '', '公允价值变动',
 '非主营金融资产收益通常剔除。', '', 'exclude', '', ''),
('nonrecurring.hedging', 'keep', 'enum', '', '套期保值',
 '为原材料或产品风险管理且持续发生的保留；投机性或一次性交易剔除。', '', 'keep', '', ''),
('nonrecurring.impairment', 'case_by_case', 'enum', '', '资产减值',
 '不机械剔除。反映产能过剩、资产竞争力下降或长期盈利恶化的不加回；有充分证据证明一次性、非重复且不代表持续经营能力下降的，可以加回。', '', 'case_by_case', '', ''),
('nonrecurring.restructuring', 'case_by_case', 'enum', '', '重组费用',
 '一次性且不反映持续经营能力的可加回。', '', 'case_by_case', '', ''),
('nonrecurring.related_party', 'manual_review', 'enum', '', '关联交易影响',
 '若存在非市场化定价，进入人工复核。', '', 'manual_review', '', ''),
('nonrecurring.require_audit_trail', '1', 'bool', '', '调整必须留痕',
 '所有调整必须保留：原始项目、调整金额、调整方向、调整理由、来源页码、复核状态。', '', '1', '0', '1');

-- ------------------------------------------------- 叙事一致性诊断指数
-- I = 50 + 20·H + 20·C + 5·R − 10·P − 15·Q   裁剪到 [0,100]
INSERT INTO rule_config
  (key, value, value_type, industry, label_cn, description, unit, default_value, min_value, max_value)
VALUES
('index.base_score', '50', 'integer', '', '指数基准分', '诊断指数公式的常数项。', '分', '50', '0', '100'),
('index.weight_history', '20', 'integer', '', '历史兑现度权重 H', '上一年度 MD&A 前瞻性表述与下一年度实际结果的匹配程度。', '分', '20', '0', '50'),
('index.weight_current', '20', 'integer', '', '当前一致性权重 C', '本期 MD&A 主张与当期财务事实的匹配程度。', '分', '20', '0', '50'),
('index.weight_risk_shift', '5', 'integer', '', '风险披露变化权重 R', '风险词、不确定性模态词、风险段落长度与位置的变化。', '分', '5', '0', '20'),
('index.penalty_template', '10', 'integer', '', '模板化惩罚权重 P', '文本高度模板化、缺少可验证指标时的扣分。', '分', '10', '0', '30'),
('index.penalty_quality_conflict', '15', 'integer', '', '财务质量冲突权重 Q', '勾稽异常与利润-现金流背离等冲突信号。', '分', '15', '0', '30'),

('index.grade_high_min', '70', 'integer', '', '高级别下限', '分值 ≥ 该值为「一致性较高」。', '分', '70', '50', '95'),
('index.grade_low_max', '45', 'integer', '', '低级别上限', '分值 < 该值为「一致性较低」。', '分', '45', '10', '60'),

('index.min_coverage', '0.60', 'percent', '',
 '最低置信度加权覆盖率',
 '低于该值时不出分，grade=insufficient，只展示证据表与人工复核入口。',
 '比例', '0.60', '0.30', '0.90'),

('index.min_observations', '5', 'integer', '',
 '最少有效观测数',
 '进入分母的观测（supported + conflicted）少于该值时不出分。',
 '条', '5', '1', '20'),

('index.deviation_threshold', '0.20', 'percent', '',
 '数值目标的明显不一致阈值',
 '★ 仅适用于 MD&A 明确提出数值目标的情况。「需求增长」「回款改善」这类方向性表述不套用该阈值，改用下面的方向判断规则。',
 '比例', '0.20', '0.05', '0.50'),

-- ---- 方向判断规则（方案选择第 10 条）----
('index.direction.opposite_verdict', 'conflicted', 'enum', '',
 '方向相反时的判定',
 '实际方向与主张相反，直接标记为冲突，不再做幅度判断。',
 '', 'conflicted', '', ''),

('index.direction.weak_verdict', 'partial', 'enum', '',
 '方向一致但幅度偏弱时的判定',
 '标记为「部分支持」，与「完全兑现」区分开，避免把只兑现一半和全部兑现混为一谈。',
 '', 'partial', '', ''),

('index.direction.weak_ratio', '0.5', 'percent', '',
 '部分支持的幅度分界',
 '⬜ 待会计确认（A-5）｜实际幅度低于主张目标幅度（或无目标时取历史同类主张中位幅度）的该比例时，判为部分支持。方案选择 §10 只说了「幅度较弱判部分支持」，没给比例，0.5 是实现时拍的。',
 '比例', '0.5', '0.2', '0.8'),

('index.direction.incomparable_verdict', 'incomparable', 'enum', '',
 '数据不可比时的判定',
 '标记为不可判断，既不计入分母也不扣分。',
 '', 'incomparable', '', ''),

-- ⚠ 下面三条权重直接决定 H / C 的数值。**冲突的负权重定多重，等于定了
--   「一次冲突扣多少分」**，而 A-7 只给了公式里的 20 分总权重，没给状态权重。
--   全部为 `⬜ 待会计确认`，见 `待会计确认.md` A-5。
('index.weight.supported', '1.0', 'percent', '', '支持观测的计分权重',
 '⬜ 待会计确认（A-5）｜各状态在指数分子中的权重。A-7 未给状态权重，1.0 是实现时拍的。',
 '倍', '1.0', '0.5', '1.5'),
('index.weight.partial', '0.5', 'percent', '', '部分支持观测的计分权重',
 '⬜ 待会计确认（A-5）｜部分支持按半权计入。A-7 未给，0.5 是实现时拍的。',
 '倍', '0.5', '0.0', '1.0'),
('index.weight.conflicted', '-1.0', 'percent', '', '冲突观测的计分权重',
 '⬜ 待会计确认（A-5）｜冲突为负权重，负多重等于「一次冲突扣多少分」。A-7 未给，−1.0 是实现时拍的。',
 '倍', '-1.0', '-1.5', '-0.5'),

('index.grade_is_display_only', '1', 'bool', '',
 '分级仅作展示',
 '固定为 1。70/45 的分级线只用于展示，**不得宣称具有普遍预测意义**，页面须明示这一点。',
 '', '1', '0', '1'),

-- ------------------------------------------------- 传导至估值情景
-- 指数 → 情景权重与参数调整。纯函数产出，绝不产出目标价。
--
-- ⚠ **这一整组都没有会计文档依据。** 五份 Word（大框架 / 具体步骤 / 方案选择 /
--   accounting_signoff_v1 / exclusion_terms）对传导只描述了**方向**
--   （大框架原话：「保持基准情景；可提高基准情景权重」），一个具体数字都没有。
--   这些数字是 2026-09-21 搭骨架时先拍的，却因为 tier 默认 hard 而看起来像会计口径。
--   **它们直接决定估值区间。** 见 `待会计确认.md` A-4。
('mapping.high.weights', '[0.60,0.25,0.15]', 'enum', '',
 '一致性较高时的情景权重',
 '⬜ 待会计确认（A-4）｜顺序为 基准/乐观/压力。文档只说「可提高基准情景权重」，未给数值。',
 '', '[0.60,0.25,0.15]', '', ''),

('mapping.high.delta', '{"revenue_growth":"historical_median","wacc":0,"terminal_growth":0}', 'enum', '',
 '一致性较高时的参数调整',
 '⬜ 待会计确认（A-4）｜增长取历史中位数，WACC 与终值增长率不变。文档未给数值。',
 '', '{"revenue_growth":"historical_median","wacc":0,"terminal_growth":0}', '', ''),

('mapping.medium.weights', '[0.50,0.20,0.30]', 'enum', '',
 '部分冲突时的情景权重',
 '⬜ 待会计确认（A-4）｜文档只说「或提高压力情景权重」，未给数值。',
 '', '[0.50,0.20,0.30]', '', ''),

('mapping.medium.delta', '{"revenue_growth":"historical_p25","wacc":0.005,"terminal_growth":0}', 'enum', '',
 '部分冲突时的参数调整',
 '⬜ 待会计确认（A-4）｜收入增速取历史下四分位，WACC 上调 0.5 个百分点。文档只说「降低增长假设」，未给数值。',
 '', '{"revenue_growth":"historical_p25","wacc":0.005,"terminal_growth":0}', '', ''),

('mapping.low.weights', '[0.35,0.15,0.50]', 'enum', '',
 '一致性低时的情景权重',
 '⬜ 待会计确认（A-4）｜文档只说「扩大估值区间」，未给数值。',
 '', '[0.35,0.15,0.50]', '', ''),

('mapping.low.delta', '{"revenue_growth":"historical_p25_x0.9","wacc":0.010,"terminal_growth":-0.005}', 'enum', '',
 '一致性低时的参数调整',
 '⬜ 待会计确认（A-4）｜增速取下四分位再打九折，WACC 上调 1 个百分点，终值增长率下调 0.5 个百分点。文档未给数值。',
 '', '{"revenue_growth":"historical_p25_x0.9","wacc":0.010,"terminal_growth":-0.005}', '', ''),

('mapping.insufficient.delta', '{}', 'enum', '',
 '证据不足时不做传导',
 'grade=insufficient 或不可比占多数时，**不自动改变任何估值参数**，只增加人工复核提示。',
 '', '{}', '', '');

-- ------------------------------------------------- 叙事主张的抽取与验证（docs/04 观测层）
-- 这几个参数决定**观测**怎么产生，不参与指数打分。指数要等 H/C/R/P/Q 五项齐全。
--
-- ⚠ 这里**曾经有一条 `narrative.min_rel_change`（默认 0.01）**，作用是「相对变动
-- 低于 1% 就当作没动」。它已于 2026-09-26 删除，因为它和会计口径直接冲突：
--   · accounting_signoff_v1.docx A-7：「方向性主张若实际方向相反，直接标记为冲突」
--   · 方案选择.docx 第 10 条：「20 个百分点」只适用于明确提出数值目标的主张
-- 判据只认方向，幅度不在其中。加那道闸门的理由（挡 0.04 个百分点那种噪声）
-- 成立，但位置错了——噪声该在「这句话够不够格被当成主张」那一层挡（词表与
-- 排除词），不该在判定这一层，否则一条真实的反向主张会因为「动得不够多」而
-- 免于被记成冲突，而它在界面上与「数据缺失」长得一模一样。
-- 数值目标的 20 个百分点阈值仍保留在 index.deviation_threshold。
INSERT INTO rule_config
  (key, value, value_type, industry, label_cn, description, unit, default_value, min_value, max_value)
VALUES
('narrative.forward_verifies_next_year', '1', 'bool', '',
 '前瞻表述验证下一年',
 'MD&A 前瞻段（经营计划、未来发展讨论）说的是下一年，验证对象因此往后挪一年。置 0 则一律用当年数据验证——那会把尚未到期的计划全部误判为冲突。★ 佐证：签字文档 A-7 对 H（历史兑现度）的定义是「上一年度 MD&A 前瞻性表述与下一年度实际结果的匹配程度」，即验下一年。',
 '', '1', '0', '1');

-- ------------------------------------------------- 关于 tier='model' 的参数
--
-- 签字文档 A-8 把「模型名、Prompt 版本、温度、输出长度」归为模型参数。
-- 它们**不写在本文件里**，而是每次运行记进 run_manifest（模型名与版本、
-- prompt_registry_hash、随机种子），因为它们的正确取值取决于运行时实际用的是
-- 哪个模型和哪一版 Prompt——把 'deepseek-chat'、温度 0 这类值固化在种子文件里，
-- 等于给出一份「看起来已经定下来、实际没人确认过」的配置，正是本项目
-- 「拒绝优于猜测」要避免的。
--
-- tier='model' 这个取值仍然保留：将来若有确实需要落库、需要版本留痕的模型侧
-- 参数，加行时写上 tier 即可，不必改表结构。
