-- =============================================================================
-- 规则参数初值
--
-- 这些是 docs/03-valuation-rules.md 与 docs/04-index-rules.md 的机读版本。
-- 页面提供「查看 / 修改 / 恢复默认」入口，每次变更写入 rule_config_version，
-- 并记入 run_manifest.rule_config_version，保证「同一输入可复现同一结果」。
--
-- industry = '' 表示全局默认；填具体行业（steel / energy）则只覆盖该行业。
-- 新增行业只需加一组行，**不需要改代码**。
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

('normalization.phase_min_spread', '0.02', 'percent', '',
 '高低盈利阶段的最小落差',
 '窗口必须同时包含**显著高于**与**显著低于**自身中枢的年份，否则判 incomplete_cycle、拒绝出分。判定用相对中枢的绝对落差（默认 2 个百分点），**不用分位数**——任何窗口都能找出「最高的那年」和「最低的那年」，用分位数判定恒为真，拦不住任何东西。绝对落差才能挡住「全是平庸年份」的窗口。',
 '比例', '0.02', '0.01', '0.10'),

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
('valuation.terminal_growth_max', '0.02', 'percent', '',
 '终值增长率上限',
 '周期行业不得给高永续增速，上限取长期通胀水平。',
 '比例', '0.02', '0.00', '0.03'),

('valuation.wacc_min', '0.06', 'percent', '',
 'WACC 下限',
 '防止压力情景下 WACC 被推到不切实际的水平。',
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
('index.weight_risk_shift', '5', 'integer', '', '风险披露充分度权重 R',
 'R = 风险披露检查项中「说明了具体风险对象、作用路径，且给出指标或事件依据」的项数 / 适用项数，取值 [0,1]。钢铁初赛固定四项：需求及钢价、原燃料成本、环保及产能约束、流动性及回款。**不得以风险词频或段落长度计分**——披露得充分不代表风险小，词频高恰恰可能是模板化套话。',
 '分', '5', '0', '20'),
('index.penalty_template', '10', 'integer', '', '缺乏可验证性的实质表述惩罚权重 P',
 'P = 经人工确认「缺乏可验证对象、期间或结果」的实质经营表述数 / 去重后的实质经营表述总数，取值 [0,1]。**排除标题、法律声明与一般背景介绍**；重复句先去重。不得因正常模板文字或未兑现的长期愿景自动扣分——「努力、力争、拟、计划」等意向不构成保证承诺。',
 '分', '10', '0', '30'),
('index.penalty_quality_conflict', '15', 'integer', '', '财务质量冲突惩罚权重 Q',
 'Q = 已确认触发财务质量检查的独立事项数 / 已完成核验的适用检查项数，取值 [0,1]。适用项**未核完则 Q 不完整**，闸门不过、不出分——不得把未核验当作未触发。去重：已在 H / C 中扣分的同一事项，Q 不再重复扣分（保留已核验分母并记录去重原因）。',
 '分', '15', '0', '30'),

('index.grade_high_min', '70', 'integer', '', '高级别下限', '分值 ≥ 该值为「一致性较高」。', '分', '70', '50', '95'),
('index.grade_low_max', '45', 'integer', '', '低级别上限', '分值 < 该值为「一致性较低」。', '分', '45', '10', '60'),

('index.min_coverage', '0.60', 'percent', '',
 '最低证据覆盖率',
 '覆盖率 = n / N。**是普通计数比，不做置信度加权**（旧稿写的「置信度加权覆盖率」是另一回事，已按 v1.1 更正）。N = 已到验证期、对象和目标可识别、且属于本期预定研究范围的去重主张数；n = 其中已完成同口径验证、拿到 supported / neutral / contradicted 的条数。未披露直接数据、仍待复核的主张留在 N 里；未到期的计划不进 N，单独披露。',
 '比例', '0.60', '0.30', '0.90'),

('index.min_observations', '5', 'integer', '',
 '最少有效观测数',
 'n（supported + neutral + contradicted）少于该值时不出分。5 条只是**演示最低门槛**，页面必须同时显示样本数 n 和覆盖率，不能展示虚构的统计置信度。',
 '条', '5', '1', '20'),

('index.deviation_threshold', '0.20', 'percent', '',
 '方向性主张的幅度参考线',
 '⚠ 保留仅为向后兼容，**判定不再用它**。会计口径 v1.1 §A.5：方向性表述改用 narrative.* 的分类型噪声区间；而明确数值目标要按原目标自身的上下限、口径和披露精度核验，**不套用统一的 20 个百分点容差**——「增长至少 5%」实际 4% 必须判未达成，不能因为只差 1 个百分点就当作无明显变化。',
 '比例', '0.20', '0.05', '0.50'),

-- ---- 判定权重与噪声区间（会计口径 v1.1 §A.2 / §A.5）----
-- 旧稿的 index.direction.* 四个键已删除：判定的语义现在由 app/schemas/enums.py::MatchVerdict
-- 承载，幅度分界改由下面 narrative.* 的分类型噪声区间决定（会计口径 v1.1 §A.2 / §A.5）。

('index.weight.supported', '1.0', 'percent', '', '支持观测的计分权重',
 's = +1。H、C 各自为其计分观测的**等权平均**，取值域 [-1,1]。', '倍', '1.0', '0.5', '1.5'),
('index.weight.neutral', '0.0', 'percent', '', '无明显变化观测的计分权重',
 's = 0。方向性主张对应的变化落在噪声区间内。**这个 0 是 v1.1 最关键的一处修订**：旧稿把中性编码成 0.5，于是 H=C=0.5 会算出 50 + 20×0.5 + 20×0.5 = 70 分，正好压在「一致性较高」的分界线上——全中性的公司反而得分最高。',
 '倍', '0.0', '-0.5', '0.5'),
('index.weight.contradicted', '-1.0', 'percent', '', '相悖观测的计分权重',
 's = -1。直接证据超过阈值且方向相反，或明确数值目标未达成。', '倍', '-1.0', '-1.5', '-0.5'),

-- 噪声区间（会计口径 v1.1 §A.5）。**边界算噪声，严格大于才触发**。
-- 阈值按指标类型分派，不许共用一个——金额用相对变化、比例用绝对变化，
-- 混用会让个别类型的判定宽出两个数量级，而且不会报错。
('narrative.min_rel_change', '0.01', 'percent', '',
 '金额 / 数量类的噪声区间',
 '基期为正且不接近零时，(本期−基期)/基期的绝对变化率不超过该值为噪声。**仅适用于金额、数量类指标**，不能套用到比例或天数上。注意：这不是审计重要性水平，只是本项目的初赛变化过滤参数。基期 ≤ 0、或基期绝对额不超过同期营业收入 0.1% 时，不自动计算增长方向。',
 '比例', '0.01', '0.001', '0.05'),
('narrative.min_ratio_change', '0.005', 'percent', '',
 '比例类的噪声区间',
 '毛利率、费用率、占比等**以 0–1 存储**的比例指标，绝对变化不超过该值（0.5 个百分点）为噪声。⚠ 若比例被存成 10.00 而不是 0.10，这个区间会宽出 200 倍，所有判定被静默中和——引擎必须断言 0 ≤ 比例 ≤ 1。',
 '比例', '0.005', '0.001', '0.02'),
('narrative.min_days_change', '3', 'decimal', '',
 '周转天数类的噪声区间',
 '应收 / 存货周转天数的绝对变化不超过该值（3 天）为噪声。',
 '天', '3', '1', '10'),
('narrative.min_utilization_change', '0.01', 'percent', '',
 '产能利用率方向的噪声区间',
 '产能利用率变化的绝对值不超过该值（1 个百分点）为噪声。',
 '比例', '0.01', '0.005', '0.05'),
('narrative.forward_verifies_next_year', '1', 'bool', '',
 '前瞻主张用下一年度验证',
 '为 1 时，某年年报里对**下一年**的表述，用下一年的实际结果验证；目标期间尚未到来的主张标记 pending，不进分母、不判冲突。搞反了会产生事后偏见的「处处支持」，且不报错——这是 docs/00 §八 警告的未来信息泄漏。',
 '', '1', '', ''),

('index.grade_is_display_only', '1', 'bool', '',
 '分级仅作展示',
 '固定为 1。70/45 的分级线只用于展示，**不得宣称具有普遍预测意义**，页面须明示这一点。',
 '', '1', '0', '1'),

-- ------------------------------------------------- 传导至估值情景
-- 指数 → 情景权重与参数调整。纯函数产出，绝不产出目标价。
-- 指数 → 估值情景的传导（会计口径 v1.1 §A.7）。
-- ⚠ v1.1 改了这里，且**优先于 docs/03-valuation-rules.md §八**：
--   初赛默认「指数提示 + 人工确认经营假设」，不把分数换算成概率或折现率。
--   WACC 与永续增长率需要独立依据，**不得仅因叙事指数低而修改**。
--   旧稿的 delta 里硬编码了 wacc +0.5/+1.0 个百分点、terminal_growth −0.5 个百分点，已移除。
-- 传导只作用于**有直接证据的经营参数**：销量影响收入、单位成本影响毛利及 EBIT、
-- 回款天数影响应收与营运资本。每次调整记录旧值、新值、依据、批准人和规则版本。
-- 同一风险**不得同时**压低收入、利润率和抬高 WACC（重复惩罚）。

('mapping.high.weights', '[0.60,0.25,0.15]', 'enum', '',
 '一致性较高时的情景权重',
 '顺序为 基准/乐观/压力。**这是研究者设定的主观假设，不是模型估计的概率**，页面须如此标注。I ≥ 70 时保留原有情景，**不得自动提高增长率**。',
 '', '[0.60,0.25,0.15]', '', ''),

('mapping.high.delta', '{"revenue_growth":"historical_median","requires_human_confirmation":true}', 'enum', '',
 '一致性较高时的参数建议',
 '增长取历史中位数。**仅供人工确认，不自动写入估值**。',
 '', '{"revenue_growth":"historical_median","requires_human_confirmation":true}', '', ''),

('mapping.medium.weights', '[0.50,0.20,0.30]', 'enum', '',
 '部分冲突时的情景权重',
 '45 ≤ I < 70：展示争议项与经营假设敏感性，**请求确认相关假设，不自动改值**。',
 '', '[0.50,0.20,0.30]', '', ''),

('mapping.medium.delta', '{"revenue_growth":"historical_p25","requires_human_confirmation":true}', 'enum', '',
 '部分冲突时的参数建议',
 '收入增速建议取历史下四分位。**需人工确认后才重算**；WACC 不因指数而变。',
 '', '{"revenue_growth":"historical_p25","requires_human_confirmation":true}', '', ''),

('mapping.low.weights', '[0.35,0.15,0.50]', 'enum', '',
 '一致性低时的情景权重',
 'I < 45：优先展开压力情景与反证条件，**人工确认具体经营参数后重算**。',
 '', '[0.35,0.15,0.50]', '', ''),

('mapping.low.delta', '{"revenue_growth":"historical_p25_x0.9","requires_human_confirmation":true}', 'enum', '',
 '一致性低时的参数建议',
 '增速建议取下四分位再打九折。**需人工确认后才重算**；WACC 与永续增长率须另有依据，不得仅因指数低而下调。',
 '', '{"revenue_growth":"historical_p25_x0.9","requires_human_confirmation":true}', '', ''),

('mapping.insufficient.delta', '{}', 'enum', '',
 '证据不足时不做传导',
 'grade=insufficient_evidence 或不可比占多数时，**不发生由指数驱动的估值调整**，只展示缺口、补资料入口与人工复核提示。',
 '', '{}', '', '');

-- ------------------------------------------------- Q 项的财务质量检查清单（会计口径 v1.1 §A.3）
--
-- 初赛固定三项。这三条是**需复核的风险筛查条件，不是造假判据**——触发只表示
-- 该查一查。周转天数口径：平均余额 ÷ 流量 × 365，取期初期末平均而非单一时点值。
--
-- ⚠ Q 的分母是「**已完成核验**的适用检查项数」。适用项没核完则 Q 不完整，
--   闸门不过、不出分——不得把「未核验」当作「未触发」。
INSERT INTO rule_config
  (key, value, value_type, industry, label_cn, description, unit, default_value, min_value, max_value)
VALUES
('quality.cfo_negative_ni_positive', '1', 'bool', '', 'Q1：连续两年经营现金流为负而净利润为正',
 '连续两个完整年度经营现金流净额为负，且这两个年度净利润均为正。', '', '1', '', ''),
('quality.receivable_days_and_aging', '1', 'bool', '', 'Q2：应收周转天数与账龄同时恶化',
 '应收账款周转天数同比增加超过 10 天，**且**账龄超过一年的应收账款占比增加超过 0.5 个百分点。⚠ 本数据包的 743 条事实里没有账龄明细，所以这一项**必然核验不完** → Q 不完整 → 闸门不过、不出分。这是正确的结果，不许拿代理指标顶替。应收票据与应收款项融资单独展示；存在重大票据结算变化的先复核可比性。', '', '1', '', ''),
('quality.inventory_days_and_sales_volume', '1', 'bool', '', 'Q3：存货周转天数上升且同口径销量下降',
 '存货周转天数同比增加超过 10 天，**且**同口径钢材销量下降超过 1%。', '', '1', '', ''),
('quality.turnover_days_gap', '10', 'decimal', '', '周转天数恶化阈值',
 'Q2、Q3 共用的同比增加阈值（天）。', '天', '10', '5', '30'),
('quality.aging_share_gap', '0.005', 'percent', '', '长账龄应收占比恶化阈值',
 'Q2 中账龄超过一年的应收账款占比增加的阈值（0.5 个百分点）。', '比例', '0.005', '0.001', '0.02'),
('quality.sales_volume_decline', '0.01', 'percent', '', '销量下降阈值',
 'Q3 中同口径钢材销量下降的阈值（1%）。', '比例', '0.01', '0.001', '0.05'),
('quality.min_balance_years', '2', 'integer', '', 'Q1 要求的连续年度数',
 '经营现金流为负、净利润为正须连续满足的完整年度数。', '年', '2', '2', '3');
