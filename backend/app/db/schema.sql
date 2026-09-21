-- =============================================================================
-- 财报叙事一致性分析与情景估值投研工作台 —— 数据库结构
--
-- 引擎：SQLite（Python 标准库 sqlite3，无需安装任何 SQL 软件）
-- 版本：0001
--
-- 设计要点：
--   1. 金额一律以 TEXT 存 Decimal 字符串，禁止 REAL/float —— 财务计算不允许
--      浮点误差。展示层负责格式化，引擎层负责量化。
--   2. 时间一律 ISO8601 TEXT，由调用方传入，禁止依赖数据库的 now()，以保证
--      「同输入必同输出」的可复现性。
--   3. 三条硬规则直接落到 CHECK 约束上，而不是只写在文档里：
--        规则一  无来源不得进已验证
--        规则二  缺失即无行（不设默认值，绝不插补 0）
--        规则三  不可比必须写原因
--      配套 v_fact_verified / v_fact_grid 两个视图，让不可信数据在 SQL 层
--      就进不了指数与估值。
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- =============================================================================
-- 一、项目与文件
-- =============================================================================

CREATE TABLE project (
  project_id     TEXT PRIMARY KEY,                 -- ULID
  name           TEXT NOT NULL,
  company_name   TEXT NOT NULL,
  stock_code     TEXT NOT NULL,                    -- '600019.SH'
  industry       TEXT NOT NULL,                    -- 'steel' / 'coal' / 'petrochem'
  base_currency  TEXT NOT NULL DEFAULT 'CNY',
  fiscal_years   TEXT NOT NULL,                    -- JSON 数组，如 [2022,2023,2024]
  base_scope     TEXT NOT NULL DEFAULT 'consolidated'
                 CHECK (base_scope IN ('consolidated','parent')),
  status         TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','archived')),
  created_at     TEXT NOT NULL
);

CREATE TABLE file (
  file_id       TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  role          TEXT NOT NULL
                CHECK (role IN ('annual_report','half_year','quarterly','announcement',
                                'comparable','research_draft','industry_data')),
  period        TEXT NOT NULL,                     -- '2024' / '2024H1' / '2024Q3'
  -- 只存相对 DATA_ROOT 的路径。绝对路径禁止入库，防止越权读取。
  rel_path      TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  bytes         INTEGER NOT NULL,
  page_count    INTEGER,
  is_scanned    INTEGER NOT NULL DEFAULT 0 CHECK (is_scanned IN (0,1)),
  parse_status  TEXT NOT NULL DEFAULT 'pending'
                CHECK (parse_status IN ('pending','parsing','parsed','failed')),
  parse_error   TEXT,
  uploaded_at   TEXT NOT NULL,
  UNIQUE (project_id, sha256)                      -- 重复上传识别
);

CREATE TABLE document_page (
  page_id         TEXT PRIMARY KEY,
  file_id         TEXT NOT NULL REFERENCES file(file_id) ON DELETE CASCADE,
  page_no         INTEGER NOT NULL,                -- PDF 物理页序，1-based
  -- 页脚印刷页码常与物理页序不一致，必须分开存，否则证据面板会指错页
  printed_page_no TEXT,
  text            TEXT,
  text_source     TEXT NOT NULL CHECK (text_source IN ('native','ocr')),
  ocr_confidence  REAL CHECK (ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)),
  has_table       INTEGER NOT NULL DEFAULT 0 CHECK (has_table IN (0,1)),
  image_path      TEXT,                            -- 页面截图，供「查看原页」
  width_pt        REAL,
  height_pt       REAL,
  UNIQUE (file_id, page_no)
);

-- 中文用 trigram 分词；unicode61 对 CJK 子串检索几乎无效（整段中文会被当成一个词元）。
--
-- ⚠ trigram 的查询长度下限是 3 个字符：短于 3 字的 MATCH **静默返回 0 条**，
--   不报错也不警告。中文财务术语里「存货」「商誉」「营业」「费用」「收入」「成本」
--   这类两字词极多，直接查 page_fts 会让它们永远搜不出来，且看起来像是年报里没有。
--   ⇒ 检索一律走 app/retrieval/fts.py 的 search_pages()，它会在短查询时回退到 LIKE。
CREATE VIRTUAL TABLE page_fts USING fts5(
  text,
  page_id UNINDEXED,
  file_id UNINDEXED,
  tokenize = 'trigram'
);

-- FTS5 是独立表，不会自动跟着 document_page 变化。没有触发器的话，写入页面后
-- 全文索引始终为空，检索静默返回 0 条 —— 不报错，只是查不到，很难排查。
--
-- 用 document_page 的隐式 rowid 作为 FTS 的 rowid：按 rowid 删除在 FTS5 里是
-- 常数时间，按 page_id（UNINDEXED 列）删除则要全表扫描。
-- ⚠ 注意：没有 INTEGER PRIMARY KEY 的表，rowid 可能被 VACUUM 重排。若执行过
--   VACUUM，需重建索引：INSERT INTO page_fts(page_fts) VALUES('rebuild');
CREATE TRIGGER trg_page_fts_insert AFTER INSERT ON document_page
BEGIN
  INSERT INTO page_fts(rowid, text, page_id, file_id)
  VALUES (new.rowid, COALESCE(new.text, ''), new.page_id, new.file_id);
END;

CREATE TRIGGER trg_page_fts_update AFTER UPDATE ON document_page
BEGIN
  DELETE FROM page_fts WHERE rowid = old.rowid;
  INSERT INTO page_fts(rowid, text, page_id, file_id)
  VALUES (new.rowid, COALESCE(new.text, ''), new.page_id, new.file_id);
END;

CREATE TRIGGER trg_page_fts_delete AFTER DELETE ON document_page
BEGIN
  DELETE FROM page_fts WHERE rowid = old.rowid;
END;

-- =============================================================================
-- 二、字段字典
-- =============================================================================

CREATE TABLE metric_definition (
  metric_key      TEXT PRIMARY KEY,                -- 'revenue'
  label_cn        TEXT NOT NULL,                   -- '营业收入'
  aliases         TEXT NOT NULL,                   -- JSON 数组，行名映射用；由会计同学维护
  -- disclosure = 披露事项：审计意见、会计政策变更这类**没有数值**的抽取目标。
  -- 它们照样要出现在字典里（否则解析层不知道那些句子值得抽），但不进 financial_fact。
  statement       TEXT NOT NULL
                  CHECK (statement IN ('balance','income','cashflow','indicator','industry','disclosure')),
  value_type      TEXT NOT NULL
                  CHECK (value_type IN ('stock','flow','ratio','text')),   -- 时点/期间/比率/文本
  unit_kind       TEXT NOT NULL
                  CHECK (unit_kind IN ('currency','percent','shares','days','ton','quantity','text')),
  sign_convention TEXT NOT NULL
                  CHECK (sign_convention IN ('positive_is_good','negative_is_good','neutral')),
  -- 非经常性损益类须单独列示，是扣非净利润对照的基础
  is_nonrecurring INTEGER NOT NULL DEFAULT 0 CHECK (is_nonrecurring IN (0,1)),
  -- 需计算而非直取（如扣非净利润、吨钢毛利）
  is_derived      INTEGER NOT NULL DEFAULT 0 CHECK (is_derived IN (0,1)),
  -- 行业专属指标标记，NULL 表示通用指标
  industry        TEXT,
  parent_key      TEXT REFERENCES metric_definition(metric_key),  -- 「其中：」层级
  display_order   INTEGER,
  note            TEXT,

  -- ---- 以下五列由会计/金融同学逐条审校（方案选择第 8 条）----
  -- 排除词：PDF 行名命中这些词时**不得**映射到本字段。JSON 数组，可为空。
  -- 三个典型来源：
  --   ① 同名的量纲变体——'净利润' 必须排除 '净利润率'/'净利润增长率'/'净利润预测'
  --   ② 相邻行名——'利息费用' 必须排除 '利息收入'，'资产处置收益' 必须排除 '营业外收入'
  --   ③ 合计行与明细行——'应收账款' 必须排除 '应收票据及应收账款'（那是合并列示行，
  --      含应收账款本身，两边都收会重复计算）
  -- 排除词漏一个，「营业成本率」「营业总成本」这类行就会被当成「营业成本」收下——
  -- 两者都是数字，不会报错，只会让毛利率静默错成另一个量级。
  exclusion_terms TEXT,
  -- 真实年报例句：从样例年报里摘一句原文，作为别名维护的锚点，也便于评审核对
  example_sentence TEXT,
  -- 例句来源。**synthetic_example 不得当作真实证据**：
  --   annual_report     已从样例年报摘录原文
  --   synthetic_example 仍是带【数值】占位符的标准句，仅供解析器回归测试
  example_source  TEXT
                  CHECK (example_source IS NULL OR
                         example_source IN ('annual_report','synthetic_example')),
  -- 合并/母公司口径提示：本字段在年报中通常以何种口径出现
  scope_note      TEXT
);

-- =============================================================================
-- 三、核心：财务事实表（十个契约字段的落库形态）
--
--   metric  -> metric_key
--   value   -> value_millions（统一百万元）
--   unit    -> unit
--   period  -> period
--   scope   -> scope
--   source_file / source_page / source_text -> 同名字段
--   confidence -> confidence
--   status  -> status
-- =============================================================================

CREATE TABLE financial_fact (
  fact_id        TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  company_id     TEXT NOT NULL,                    -- 主公司或 comparable_company
  is_primary     INTEGER NOT NULL CHECK (is_primary IN (0,1)),

  metric_key     TEXT NOT NULL REFERENCES metric_definition(metric_key),

  -- 统一换算为百万元后的值。TEXT 存 Decimal 字符串，禁止 REAL。
  value_millions TEXT,
  -- 原始披露数字与原始单位永不覆盖，保留可追溯的换算过程
  value_raw      TEXT,
  raw_unit       TEXT,
  unit_factor    TEXT,
  unit           TEXT NOT NULL DEFAULT '百万元',

  period         TEXT NOT NULL,                    -- '2024' / '2024H1' / '2024-12-31'
  -- 取值描述的是**这笔数值本身的性质**，不是它出现在哪份报告里：
  --   current  本期发生额（利润表、现金流量表的期间数）
  --   instant  期末时点余额（资产负债表）
  --   opening  期初时点余额
  --   average  期间平均值
  --
  -- ⚠ 刻意不含 'prior'。FY2023 的数字，无论出现在 2023 年报的「本期」栏还是
  --   2024 年报的「上期」栏，都是同一笔经济事实：period 都是 '2023'、kind 都是
  --   'current'。若把「上期」也做成一个 kind，同一笔事实会按两种 kind 各存一行，
  --   任何 SUM/AVG 聚合都会重复计算，而且不会报错。
  --   数据来自哪份报告由 source_file_id / extractor 记录；同一事实的多个来源
  --   由 fact_observation 承载。
  period_kind    TEXT NOT NULL
                 CHECK (period_kind IN ('current','instant','opening','average')),
  period_start   TEXT,
  period_end     TEXT,

  scope          TEXT NOT NULL CHECK (scope IN ('consolidated','parent')),

  source_file    TEXT NOT NULL,
  source_file_id TEXT NOT NULL REFERENCES file(file_id),
  source_page    INTEGER NOT NULL,
  source_printed_page TEXT,
  source_table   TEXT,                             -- '合并现金流量表'
  source_row_label TEXT,                           -- 原始行名，保留原始标签
  source_text    TEXT NOT NULL,                    -- 原文片段
  bbox           TEXT,                             -- JSON 坐标，供前端高亮

  confidence     REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  status         TEXT NOT NULL
                 CHECK (status IN ('validated','needs_review','not_found','rejected')),

  -- 重述值与原值双行并存，永不 UPDATE 覆盖
  restated       INTEGER NOT NULL DEFAULT 0 CHECK (restated IN (0,1)),
  restatement_note TEXT,

  comparable     INTEGER NOT NULL DEFAULT 1 CHECK (comparable IN (0,1)),
  incomparable_reason TEXT
                 CHECK (incomparable_reason IS NULL OR incomparable_reason IN
                        ('mna','restatement','seasonality','policy_change','scope_change','industry_cycle')),

  extractor      TEXT NOT NULL,                    -- 'rule:v3' / 'llm:deepseek-chat@<prompt_hash>' / 'human:<uid>'
  created_at     TEXT NOT NULL,

  -- ★★ 硬规则二：缺失即无行 —— value_millions 无默认值，且下方规则一要求
  --    validated 行必须有值。系统绝不插补 0；「未找到」由 v_fact_grid 视图生成。
  --
  -- ★★ 硬规则一：无来源不得进已验证
  CHECK (status <> 'validated' OR (
           value_millions IS NOT NULL
       AND source_text IS NOT NULL
       AND length(trim(source_text)) > 0
       AND source_page > 0
       AND unit IS NOT NULL
       AND period IS NOT NULL
       AND scope IS NOT NULL
  )),

  -- ★★ 硬规则三：不可比必须写原因
  CHECK (comparable = 1 OR incomparable_reason IS NOT NULL)
);

CREATE UNIQUE INDEX ux_fact_natural ON financial_fact
  (project_id, company_id, metric_key, period, period_kind, scope, restated);
CREATE INDEX ix_fact_metric_period ON financial_fact(project_id, metric_key, period);
CREATE INDEX ix_fact_company ON financial_fact(project_id, company_id);

-- 同一笔事实在年报里的每一次出现。
--
-- 为什么单独建表：financial_fact 的自然键唯一确定一笔经济事实，所以一个数字只能
-- 存一行；但现实中它往往同时出现在「主要会计数据」表、三张主表、附注和正文里，
-- 精度还常常不同（正文写「约 123.5 亿元」，主表写 12,345,678,901.23 元）。
-- 这些出现各有各的出处，硬塞进一行就丢了证据；各存一行又会让聚合重复计算。
--
-- 用法：解析阶段把每次命中写入本表；随后跑交叉校验，取多数一致值写入
-- financial_fact，并把被采纳的观测标为 adopted、回填 resolved_fact_id。
-- 未被采纳的必须写明理由，禁止静默丢弃。
CREATE TABLE fact_observation (
  observation_id  TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  company_id      TEXT NOT NULL,
  metric_key      TEXT NOT NULL REFERENCES metric_definition(metric_key),
  period          TEXT NOT NULL,
  period_kind     TEXT NOT NULL
                  CHECK (period_kind IN ('current','instant','opening','average')),
  scope           TEXT NOT NULL CHECK (scope IN ('consolidated','parent')),

  value_raw       TEXT NOT NULL,               -- 原样文本，如 '12,345,678,901.23'
  raw_unit        TEXT,                        -- '元' / '万元' / '百万元'
  value_millions  TEXT,                        -- 换算后；换算失败时为 NULL

  source_file_id  TEXT NOT NULL REFERENCES file(file_id),
  source_page     INTEGER NOT NULL,
  source_table    TEXT,
  -- 同一数字出现在报表的哪个位置。位置不同可信度不同：
  -- 三张主表 > 主要指标表 > 附注 > 正文叙述。
  source_location TEXT NOT NULL
                  CHECK (source_location IN ('main_statement','indicator_table',
                                             'notes','mdna_text','other')),
  source_text     TEXT NOT NULL,
  bbox            TEXT,

  confidence      REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extractor       TEXT NOT NULL,
  created_at      TEXT NOT NULL,

  -- 校验结论
  resolution      TEXT NOT NULL DEFAULT 'pending'
                  CHECK (resolution IN ('pending','adopted','rejected')),
  resolved_fact_id TEXT REFERENCES financial_fact(fact_id),
  rejection_note  TEXT,

  -- 标为 adopted 就必须指向一条事实
  CHECK (resolution <> 'adopted' OR resolved_fact_id IS NOT NULL),
  -- 被否决的必须写明理由
  CHECK (resolution <> 'rejected'
         OR (rejection_note IS NOT NULL AND length(trim(rejection_note)) > 0)),
  -- 尚未裁决的不得提前绑定事实
  CHECK (resolution <> 'pending' OR resolved_fact_id IS NULL)
);

CREATE INDEX ix_obs_fact_key ON fact_observation
  (project_id, company_id, metric_key, period, period_kind, scope);
CREATE INDEX ix_obs_resolved ON fact_observation(resolved_fact_id);

-- 同一笔事实至多只能有一条观测被采纳为 canonical。
-- 用部分唯一索引而非普通唯一索引：没有它就可能出现两条 content 都被采纳，
-- 而 financial_fact 那边看起来完全正常，冲突要到很后面才暴露。
CREATE UNIQUE INDEX ux_obs_one_adopted ON fact_observation(
  project_id, company_id, metric_key, period, period_kind, scope
) WHERE resolution = 'adopted';

-- 人工修正只追加增量，旧行保留
CREATE TABLE fact_correction (
  correction_id TEXT PRIMARY KEY,
  fact_id       TEXT NOT NULL REFERENCES financial_fact(fact_id),
  field         TEXT NOT NULL,
  old_value     TEXT,
  new_value     TEXT,
  reason        TEXT NOT NULL,
  operator      TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

-- 每条校验结果都带公式与输入 fact_id 列表，构成完整证据链
CREATE TABLE fact_check_result (
  check_id    TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  period      TEXT NOT NULL,
  scope       TEXT NOT NULL CHECK (scope IN ('consolidated','parent')),
  rule_key    TEXT NOT NULL,                       -- 'bs_equation' / 'ni_to_cfo_bridge' / ...
  severity    TEXT NOT NULL CHECK (severity IN ('info','warn','error')),
  status      TEXT NOT NULL
              CHECK (status IN ('passed','failed','skipped_missing_data','skipped_incomparable')),
  lhs         TEXT,
  rhs         TEXT,
  diff        TEXT,
  tolerance   TEXT,
  formula     TEXT NOT NULL,
  input_facts TEXT NOT NULL,                       -- JSON: [fact_id, ...]
  message     TEXT NOT NULL,                       -- 中文可读说明
  suggestion  TEXT,
  created_at  TEXT NOT NULL
);

-- =============================================================================
-- 四、MD&A 主张与匹配
-- =============================================================================

CREATE TABLE mdna_section (
  section_id TEXT PRIMARY KEY,
  file_id    TEXT NOT NULL REFERENCES file(file_id) ON DELETE CASCADE,
  heading    TEXT,
  kind       TEXT NOT NULL
             CHECK (kind IN ('mdna','risk_disclosure','business_review','outlook','other')),
  page_from  INTEGER NOT NULL,
  page_to    INTEGER NOT NULL,
  text       TEXT NOT NULL,
  CHECK (page_to >= page_from)
);

CREATE TABLE claim (
  claim_id        TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  section_id      TEXT NOT NULL REFERENCES mdna_section(section_id) ON DELETE CASCADE,
  claim_text      TEXT NOT NULL,                   -- 原句，不改写
  subject         TEXT,                            -- 主体
  action          TEXT,                            -- 动作/状态
  object          TEXT,                            -- 对象
  period_expr     TEXT,                            -- 原文期间表述
  period_norm     TEXT,                            -- 归一化期间
  direction       TEXT CHECK (direction IN
                  ('up','down','improve','deteriorate','flat','unknown')),
  magnitude_text  TEXT,
  magnitude_value TEXT,
  magnitude_unit  TEXT,
  claim_type      TEXT NOT NULL
                  CHECK (claim_type IN ('demand','order','capacity','collection',
                                        'product_mix','cost','risk','macro','other')),
  -- 0 表示无法识别期间/对象，仅作背景展示，不进入一致性评分
  verifiable      INTEGER NOT NULL CHECK (verifiable IN (0,1)),
  background_only INTEGER NOT NULL DEFAULT 0 CHECK (background_only IN (0,1)),
  confidence      REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  source_file_id  TEXT NOT NULL REFERENCES file(file_id),
  source_page     INTEGER NOT NULL,
  source_text     TEXT NOT NULL,
  bbox            TEXT,
  extractor       TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,                   -- 可追溯
  -- 抽取这条主张的那次模型调用。指向 llm_call 才能查到当时用的 prompt 原文与参数。
  llm_call_id     TEXT REFERENCES llm_call(call_id),
  status          TEXT NOT NULL
                  CHECK (status IN ('validated','needs_review','rejected')),
  created_at      TEXT NOT NULL
);

CREATE INDEX ix_claim_project ON claim(project_id, claim_type);

-- 一条主张可对应多个候选指标
CREATE TABLE claim_indicator (
  id               TEXT PRIMARY KEY,
  claim_id         TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
  metric_key       TEXT NOT NULL REFERENCES metric_definition(metric_key),
  role             TEXT NOT NULL CHECK (role IN ('primary','supporting')),
  match_confidence REAL NOT NULL CHECK (match_confidence >= 0 AND match_confidence <= 1),
  matched_by       TEXT NOT NULL                   -- 'rule:alias' / 'llm:<prompt>' / 'human:<uid>'
);

-- 主张 × 指标 × 期间 三元组 —— 诊断指数的基本观测单位
CREATE TABLE claim_match (
  match_id        TEXT PRIMARY KEY,
  claim_id        TEXT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
  metric_key      TEXT NOT NULL REFERENCES metric_definition(metric_key),
  fact_id         TEXT REFERENCES financial_fact(fact_id),
  claim_period    TEXT NOT NULL,
  fact_period     TEXT NOT NULL,
  direction_claim TEXT,
  direction_actual TEXT,
  direction_consistent INTEGER CHECK (direction_consistent IS NULL OR direction_consistent IN (0,1)),
  magnitude_target TEXT,
  magnitude_actual TEXT,
  relative_deviation TEXT,
  -- 方向判断分四态 + 部分支持：
  --   实际方向与主张相反              -> conflicted（直接冲突）
  --   方向一致但幅度明显偏弱          -> partial（部分支持）
  --   方向一致且幅度达标              -> supported
  --   并购/重述/季节性等导致不可比    -> incomparable（不进分母，不扣分）
  --   找不到对应财务指标              -> missing（不推断为失败，转人工复核）
  verdict         TEXT NOT NULL
                  CHECK (verdict IN ('supported','partial','conflicted',
                                     'incomparable','missing')),
  reason          TEXT NOT NULL,                   -- 中文理由，页面直接展示
  confidence      REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  formula         TEXT,
  inputs          TEXT,                            -- JSON，保证可复算
  reviewer        TEXT,
  reviewed_at     TEXT,
  created_at      TEXT NOT NULL,
  -- 不可比必须写明原因（与财务事实同一原则）
  CHECK (verdict <> 'incomparable' OR (reason IS NOT NULL AND length(trim(reason)) > 0))
);

CREATE INDEX ix_match_claim ON claim_match(claim_id);
CREATE INDEX ix_match_verdict ON claim_match(verdict);

-- =============================================================================
-- 五、叙事—财务一致性诊断指数
-- =============================================================================

CREATE TABLE diagnosis_run (
  run_id              TEXT PRIMARY KEY,
  project_id          TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  task_id             TEXT,
  rule_config_version INTEGER NOT NULL,            -- 引用规则版本 → 可复现
  observation_count   INTEGER NOT NULL,
  -- 进入分母的有效观测 = supported + partial + conflicted。
  -- incomparable 与 missing 既不计入分母也不扣分，只在页面单列。
  comparable_count    INTEGER NOT NULL,
  incomparable_count  INTEGER NOT NULL,            -- 不扣分
  missing_count       INTEGER NOT NULL,            -- 不推断失败
  partial_count       INTEGER NOT NULL DEFAULT 0,  -- 方向一致但幅度偏弱
  coverage            REAL CHECK (coverage IS NULL OR (coverage >= 0 AND coverage <= 1)),
  score               TEXT,                        -- 0–100 Decimal 字符串；证据不足时为 NULL
  grade               TEXT NOT NULL
                      CHECK (grade IN ('high','medium','low','insufficient')),
  confidence          REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  insufficient_reason TEXT,
  -- 审慎表述模板，模板生成而非 LLM 生成。禁止输出「虚假披露」等超证据结论。
  conclusion_boundary TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  -- ★ 证据不足时不得出分
  CHECK (grade <> 'insufficient' OR score IS NULL)
);

-- 构成项与触发证据 —— 页面据此展示「这个分是怎么来的」
CREATE TABLE diagnosis_component (
  id           TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES diagnosis_run(run_id) ON DELETE CASCADE,
  component    TEXT NOT NULL
               CHECK (component IN ('history','current','risk_shift',
                                    'template_penalty','quality_conflict')),
  raw_value    TEXT,
  weight       TEXT,
  contribution TEXT,
  formula      TEXT NOT NULL,
  evidence_refs TEXT NOT NULL,                     -- JSON: {claim_ids,fact_ids,check_ids,match_ids}
  explanation  TEXT NOT NULL                       -- 中文说明，模板生成
);

-- 规则参数：可查看、可修改、可恢复默认
CREATE TABLE rule_config (
  key           TEXT NOT NULL,
  -- 行业维度的参数化：新增行业只加一组阈值，不改代码。
  -- 用空串表示「全局默认」而非 NULL —— SQLite 的唯一索引里 NULL 互不相等，
  -- 用 NULL 会让同一个 key 插进多行全局默认值而不报错。
  industry      TEXT NOT NULL DEFAULT '',
  value         TEXT NOT NULL,
  value_type    TEXT NOT NULL CHECK (value_type IN ('decimal','integer','percent','enum','bool')),
  label_cn      TEXT NOT NULL,
  description   TEXT NOT NULL,
  unit          TEXT,
  default_value TEXT NOT NULL,
  min_value     TEXT,
  max_value     TEXT,
  PRIMARY KEY (key, industry)
);

CREATE TABLE rule_config_version (
  version    INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  operator   TEXT NOT NULL,
  reason     TEXT,
  diff       TEXT NOT NULL,
  snapshot   TEXT NOT NULL
);

-- =============================================================================
-- 六、可比公司与行业周期基准（能源钢铁专用）
-- =============================================================================

CREATE TABLE comparable_company (
  comparable_id    TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  name             TEXT NOT NULL,
  stock_code       TEXT,
  industry         TEXT NOT NULL,                  -- 细分行业，必须与主公司一致才可比
  sub_industry     TEXT,                           -- 'steel_long' / 'steel_flat' / 'coking_coal' ...
  business_similarity TEXT NOT NULL,
  scale_note       TEXT,
  growth_note      TEXT,
  profitability    TEXT NOT NULL CHECK (profitability IN ('profitable','loss')),
  data_date        TEXT NOT NULL,
  -- 选择理由必须记录（相对估值硬规则）
  selection_reason TEXT NOT NULL,
  -- 'valuation_peer' 进估值可比集；'chain_reference' 仅作产业链参考，不进中位数
  peer_role        TEXT NOT NULL DEFAULT 'valuation_peer'
                   CHECK (peer_role IN ('valuation_peer','chain_reference')),
  pe               TEXT,
  ev_ebitda        TEXT,
  pb               TEXT,
  source           TEXT NOT NULL,
  excluded         INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0,1)),
  exclude_reason   TEXT
);

-- ---------------------------------------------------------------------------
-- 周期正常化（能源钢铁行业估值起点）
--
-- 口径（已与会计方向约定，落库为 rule_config 初值）：
--   窗口   默认 8 个完整财务年度，且必须通过「高低盈利阶段覆盖校验」；
--          8 年数据不足或周期不完整时扩展到 10 年；仍不足则**不强行正常化**。
--   核心   收入加权的周期中位 EBIT margin。
--   交叉   吨钢毛利、EBITDA margin、产能利用率、ROIC —— 仅作验证，不参与核心计算。
--
-- 硬约束：周期股不得用最近一年的高/低点外推；正常化失败时 DCF 必须拒绝计算，
--        而不是退回去用一个未经验证的假设值。
-- ---------------------------------------------------------------------------

CREATE TABLE normalization_run (
  normalization_id TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  task_id          TEXT,

  -- 窗口。主窗口 2017—2024（8 个完整年度），回退窗口 2015—2024（10 个）。
  -- 两者都锚定在「最新完整年度」上滚动：加入 2025 年报后自动变为 2018—2025 / 2016—2025，
  -- 不采用「2016—2023」这类不含最新年度的窗口，避免被质疑为人为挑选区间。
  window_mode      TEXT NOT NULL
                   CHECK (window_mode IN ('primary_8y','fallback_10y')),
  preferred_years  INTEGER NOT NULL,               -- 8
  fallback_years   INTEGER NOT NULL,               -- 10
  min_comparable_years INTEGER NOT NULL,           -- 主窗口 7；回退窗口 8
  anchor_year      TEXT,                           -- 最新完整年度，如 '2024'
  window_start     TEXT,                           -- '2017'
  window_end       TEXT,                           -- '2024'
  years_available  INTEGER,                        -- 实际取到数据的年数
  comparable_years INTEGER,                        -- 排除不可比年度后的年数

  -- 高低盈利阶段覆盖校验
  covers_high_phase INTEGER CHECK (covers_high_phase IN (0,1)),
  covers_low_phase  INTEGER CHECK (covers_low_phase IN (0,1)),
  coverage_passed   INTEGER CHECK (coverage_passed IN (0,1)),
  -- 判定阈值快照，保证同输入可复现
  high_threshold    TEXT,
  low_threshold     TEXT,
  coverage_detail   TEXT,                          -- JSON：逐年 ebit_margin 与 phase 判定

  -- EBIT 口径。三种算法并存，主 DCF 默认用 reported；
  -- 只有在会计同学批准调整后，才切换到 adjusted（见 ebit_adjustment.review_status）。
  ebit_variant      TEXT NOT NULL DEFAULT 'reported'
                    CHECK (ebit_variant IN ('reported','adjusted')),
  ebit_formula      TEXT NOT NULL,                 -- 本次实际采用的公式，如
                                                   -- '利润总额 + 利息费用 − 利息收入'
  crosscheck_formula TEXT,                         -- '营业利润 + 财务费用'
  crosscheck_max_deviation TEXT,                   -- 两法最大差异比例
  crosscheck_needs_review INTEGER CHECK (crosscheck_needs_review IN (0,1)),
                                                   -- 差异超过容差（默认 5%）时为 1

  -- 核心结果
  core_metric       TEXT NOT NULL DEFAULT 'ebit_margin',
  weighting         TEXT NOT NULL DEFAULT 'revenue_weighted',
  ebit_margin_mid   TEXT,                          -- 收入加权的周期中位 EBIT margin
  ebit_margin_p25   TEXT,
  ebit_margin_p75   TEXT,

  status            TEXT NOT NULL
                    CHECK (status IN ('normalized','extended_normalized',
                                      'NORMALIZATION_INSUFFICIENT_DATA',
                                      'incomplete_cycle')),
  insufficient_reason TEXT,

  method_version    TEXT NOT NULL,
  rule_config_version INTEGER NOT NULL,
  created_at        TEXT NOT NULL,

  -- 出分必须同时满足：通过覆盖校验、核心值非空、可比年度数达标
  CHECK (status NOT IN ('normalized','extended_normalized')
         OR (coverage_passed = 1
             AND ebit_margin_mid IS NOT NULL
             AND comparable_years >= min_comparable_years)),

  -- ★ 不强行正常化：数据不足或周期不完整时，**不得输出任何中枢值**。
  --   这比「输出一个带警告的数字」更严格 —— 下游 DCF 拿不到值就只能拒绝计算。
  CHECK (status NOT IN ('NORMALIZATION_INSUFFICIENT_DATA','incomplete_cycle')
         OR (ebit_margin_mid IS NULL
             AND ebit_margin_p25 IS NULL
             AND ebit_margin_p75 IS NULL)),

  -- 异常状态必须写明原因，禁止静默降级
  CHECK (status NOT IN ('NORMALIZATION_INSUFFICIENT_DATA','incomplete_cycle')
         OR (insufficient_reason IS NOT NULL AND length(trim(insufficient_reason)) > 0))
);

-- 窗口逐年明细，保证正常化过程可被逐项复算。
--
-- 历史年份（2015—2016）走**轻解析**：只取营业收入、利润总额、利息费用、利息收入
-- 及重大调整。最近 3 年走完整解析（74 字段 + 三表 + MD&A），两者在此表汇合。
CREATE TABLE normalization_year (
  id               TEXT PRIMARY KEY,
  normalization_id TEXT NOT NULL REFERENCES normalization_run(normalization_id) ON DELETE CASCADE,
  year             TEXT NOT NULL,

  -- ---- 轻解析的原始输入（正常化只需要这几个）----
  revenue           TEXT,                          -- 营业收入
  total_profit      TEXT,                          -- 利润总额
  interest_expense  TEXT,                          -- 利息费用
  interest_income   TEXT,                          -- 利息收入
  operating_profit  TEXT,                          -- 营业利润（交叉核对用）
  financial_expense TEXT,                          -- 财务费用（交叉核对用）

  -- ---- 三种 EBIT 并存，禁止只留一个 ----
  -- Reported   = 利润总额 + 利息费用 − 利息收入        ← 主 DCF 默认口径
  -- Crosscheck = 营业利润 + 财务费用                   ← 仅核对，超容差进 needs_review
  -- Adjusted   = Reported − 非核心收益 + 非核心损失 ± 经批准的一次性调整
  reported_ebit     TEXT,
  crosscheck_ebit   TEXT,
  adjusted_ebit     TEXT,
  crosscheck_deviation TEXT,                       -- |Crosscheck − Reported| / |Reported|
  adjustment_delta  TEXT,                          -- 调整合计（见 ebit_adjustment 明细）
  adjustment_approved INTEGER CHECK (adjustment_approved IN (0,1)),
                                                   -- 会计是否已批准；未批准时主 DCF 用 reported

  -- ---- 参与中枢计算的口径 ----
  ebit_margin       TEXT,                          -- 实际用于加权的 EBIT margin
  ebit_variant_used TEXT CHECK (ebit_variant_used IN ('reported','adjusted')),
  revenue_weight    TEXT,                          -- 该年收入 / 窗口内可比年度收入合计
  phase             TEXT CHECK (phase IN ('high','normal','low')),
  is_median_year    INTEGER CHECK (is_median_year IN (0,1)),

  -- ---- 可比性：三步流程，不自动删除 ----
  -- 1. 先标记 non_comparable；2. 会计判断；3. 确认后从主中枢排除，但敏感性分析中保留
  comparable        INTEGER NOT NULL DEFAULT 1 CHECK (comparable IN (0,1)),
  incomparable_reason TEXT
                    CHECK (incomparable_reason IS NULL OR incomparable_reason IN
                           ('mna','restructuring','asset_injection','scope_change',
                            'restatement','policy_change','industry_cycle','other')),
  reviewed_by_accounting INTEGER NOT NULL DEFAULT 0 CHECK (reviewed_by_accounting IN (0,1)),
  included_in_median INTEGER NOT NULL DEFAULT 1 CHECK (included_in_median IN (0,1)),

  -- 证据链：这一年数据的来源事实行
  revenue_fact_id  TEXT REFERENCES financial_fact(fact_id),
  ebit_fact_id     TEXT REFERENCES financial_fact(fact_id),
  note             TEXT,

  UNIQUE (normalization_id, year),
  -- 不可比必须写明原因
  CHECK (comparable = 1 OR incomparable_reason IS NOT NULL),
  -- 不可比年度一律不进主中枢（但整行保留，供敏感性分析继续使用）。
  -- 注意方向：复核是用来**确认排除**的，不是用来**批准纳入**的 —— 默认全指标为可比，
  -- 只有发现重大重组/重述等问题并标记后才排除，避免默认状态下什么都算不出来。
  CHECK (included_in_median = 0 OR comparable = 1)
);

-- EBIT 调整明细。每一笔调整都必须留下「原始项目 / 金额 / 方向 / 理由 / 来源页码 / 复核状态」。
-- 会计同学未批准前，DCF 一律使用 reported EBIT，调整结果只进敏感性分析。
CREATE TABLE ebit_adjustment (
  adjustment_id    TEXT PRIMARY KEY,
  normalization_year_id TEXT NOT NULL REFERENCES normalization_year(id) ON DELETE CASCADE,
  -- 原始项目名称，原样保留年报里的写法
  item             TEXT NOT NULL,
  category         TEXT NOT NULL
                   CHECK (category IN ('gov_subsidy','asset_disposal','investment_income',
                                       'fair_value_change','hedging','impairment',
                                       'restructuring','related_party','other')),
  amount           TEXT NOT NULL,                  -- 调整金额（正数）
  direction        TEXT NOT NULL CHECK (direction IN ('add_back','deduct')),
  reason           TEXT NOT NULL,                  -- 调整理由，必须可读
  source_file      TEXT,
  source_page      INTEGER,
  review_status    TEXT NOT NULL DEFAULT 'pending'
                   CHECK (review_status IN ('pending','approved','rejected')),
  reviewer         TEXT,
  reviewed_at      TEXT,
  created_at       TEXT NOT NULL,
  CHECK (review_status = 'pending' OR reviewer IS NOT NULL)
);

CREATE INDEX ix_ebit_adj_year ON ebit_adjustment(normalization_year_id);

-- 交叉验证项：吨钢毛利、EBITDA margin、产能利用率、ROIC。
-- **仅作参考，不参与核心计算**，用于回答「EBIT margin 中枢这个结论稳不稳」。
--
-- 各项的既定口径：
--   吨钢毛利     优先采用公司年报披露值；自算时 = (钢材销售收入 − 钢材销售成本) / 钢材销售量，
--                销售成本含材料、人工、制造费用与折旧。若只能得到「售价 − 原材料成本」，
--                必须命名为「吨钢原料差价」，不得称为吨钢毛利。
--   产能利用率   采用公司披露的「有效产能利用率」，不用设计产能。
--                一体化钢企 = 粗钢产量 / 有效粗钢产能；轧钢企业 = 钢材产量 / 有效钢材产能。
--                粗钢产能与钢材产量不得混用。只用于判断周期阶段与经营状态，不进 DCF 公式。
--   ROIC         NOPAT / 平均投入资本，其中 NOPAT = 调整后 EBIT × (1 − 正常化税率)，
--                投入资本 = 股东权益 + 有息负债 + 租赁负债 − 超额现金 − 非经营性金融投资，
--                取期初期末平均值。主口径保留商誉，扣商誉口径只作敏感性，两者不得混用。
CREATE TABLE normalization_crosscheck (
  id               TEXT PRIMARY KEY,
  normalization_id TEXT NOT NULL REFERENCES normalization_run(normalization_id) ON DELETE CASCADE,
  metric_key       TEXT NOT NULL REFERENCES metric_definition(metric_key),
  -- 同一指标可能有多套口径（如 ROIC 含商誉 / 扣商誉），分开存，禁止混用
  variant          TEXT NOT NULL DEFAULT 'primary',
  mid_cycle_value  TEXT,
  unit             TEXT,
  -- 口径说明：原样记录数据来源口径（不同公司口径不同，不可直接混算）
  basis            TEXT,
  data_source      TEXT NOT NULL
                   CHECK (data_source IN ('company_disclosed','self_calculated','external')),
  -- 是否与核心结论（EBIT margin 中枢）方向一致
  agrees_with_core INTEGER CHECK (agrees_with_core IN (0,1)),
  deviation_note   TEXT,
  -- ★ 交叉验证项一律不得影响 DCF。核心中枢只能来自 normalization_run.ebit_margin_mid。
  affects_dcf      INTEGER NOT NULL DEFAULT 0 CHECK (affects_dcf = 0),
  evidence_refs    TEXT,
  created_at       TEXT NOT NULL,
  UNIQUE (normalization_id, metric_key, variant)
);

-- 外部行业数据（可选）。仅用于交叉验证与产业链分析，不得作为 DCF 起点。
CREATE TABLE industry_benchmark (
  benchmark_id   TEXT PRIMARY KEY,
  project_id     TEXT REFERENCES project(project_id) ON DELETE CASCADE,
  industry       TEXT NOT NULL,
  metric_key     TEXT NOT NULL REFERENCES metric_definition(metric_key),
  period         TEXT NOT NULL,
  value          TEXT NOT NULL,
  unit           TEXT NOT NULL,
  source_type    TEXT NOT NULL
                 CHECK (source_type IN ('user_input','external_data','comparable_stat')),
  source         TEXT NOT NULL,                    -- 出处必须写明
  note           TEXT,
  created_at     TEXT NOT NULL
);

-- =============================================================================
-- 七、估值
-- =============================================================================

CREATE TABLE valuation_run (
  run_id             TEXT PRIMARY KEY,
  project_id         TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  task_id            TEXT,
  diagnosis_run_id   TEXT REFERENCES diagnosis_run(run_id),  -- 指数→情景的传导来源
  rule_config_version INTEGER NOT NULL,
  model              TEXT NOT NULL DEFAULT 'dcf_fcff',
  currency           TEXT NOT NULL DEFAULT 'CNY',
  forecast_years     INTEGER NOT NULL CHECK (forecast_years > 0),
  base_year          TEXT NOT NULL,
  tax_rate           TEXT NOT NULL,
  net_debt           TEXT NOT NULL,
  net_debt_source    TEXT NOT NULL,
  non_operating_assets TEXT,
  minority_interest  TEXT,
  lease_liability    TEXT,
  diluted_shares     TEXT NOT NULL,
  diluted_shares_source TEXT NOT NULL,
  terminal_method    TEXT NOT NULL DEFAULT 'gordon'
                     CHECK (terminal_method IN ('gordon','exit_multiple','both')),
  -- ★ 周期位置必须显式标注，让评委看到系统知道自己在周期的哪里
  cycle_position     TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (cycle_position IN ('peak','above_mid','mid','below_mid','trough','unknown')),
  cycle_position_basis TEXT,                       -- 判断依据
  -- 正常化盈利起点必须落在一次具体的正常化运行上，不可凭空填。
  -- 周期行业（能源钢铁）必填，且被引用那次运行的 status 必须是
  -- normalized / extended_normalized；否则 DCF 必须拒绝计算，
  -- 不得退回使用未经覆盖校验的假设值。
  normalized_basis_id TEXT REFERENCES normalization_run(normalization_id),

  -- 本次 DCF 采用的 EBIT 口径。默认 reported（报告 EBIT）；
  -- 只有在会计同学逐笔批准 ebit_adjustment 之后，才允许切到 adjusted，
  -- 且调整结果此前一直只作为敏感性分析存在。
  ebit_basis         TEXT NOT NULL DEFAULT 'reported'
                     CHECK (ebit_basis IN ('reported','adjusted')),
  exit_multiple_check TEXT,
  implied_exit_multiple TEXT,
  created_at         TEXT NOT NULL
);

CREATE TABLE valuation_scenario (
  scenario_id     TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES valuation_run(run_id) ON DELETE CASCADE,
  scenario        TEXT NOT NULL CHECK (scenario IN ('base','bull','bear')),
  -- 由指数映射规则给出，非手填
  weight          TEXT NOT NULL,
  enterprise_value TEXT,
  equity_value    TEXT,
  value_per_share TEXT,
  param_delta     TEXT NOT NULL,                   -- JSON：各参数相对基准的增减
  -- JSON：引用 diagnosis_component.id，构成「原参数—新参数—触发证据」
  trigger_refs    TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

-- 每个参数必须标记来源
CREATE TABLE valuation_param (
  param_id     TEXT PRIMARY KEY,
  scenario_id  TEXT NOT NULL REFERENCES valuation_scenario(scenario_id) ON DELETE CASCADE,
  year         INTEGER,
  name         TEXT NOT NULL,
  value        TEXT NOT NULL,
  source_type  TEXT NOT NULL
               CHECK (source_type IN ('historical_fact','user_input','comparable_stat','model_assumption')),
  source_ref   TEXT NOT NULL                       -- fact_id / comparable_id / 'user:<uid>' / 'assumption:<key>'
);

-- 预测期逐年现金流，保证估值可被 Excel 手工复算
CREATE TABLE valuation_output (
  id              TEXT PRIMARY KEY,
  scenario_id     TEXT NOT NULL REFERENCES valuation_scenario(scenario_id) ON DELETE CASCADE,
  year            INTEGER NOT NULL,
  ebit            TEXT,
  tax             TEXT,
  nopat           TEXT,
  da              TEXT,
  capex           TEXT,
  wc_change       TEXT,
  fcff            TEXT,
  discount_factor TEXT,
  pv              TEXT,
  formula         TEXT NOT NULL,
  inputs          TEXT NOT NULL
);

CREATE TABLE sensitivity_grid (
  grid_id     TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES valuation_run(run_id) ON DELETE CASCADE,
  scenario    TEXT NOT NULL CHECK (scenario IN ('base','bull','bear')),
  kind        TEXT NOT NULL
              CHECK (kind IN ('wacc_x_g','growth_x_margin','before_after')),
  axes        TEXT NOT NULL,                       -- JSON：行轴与列轴的名称与取值
  cells       TEXT NOT NULL,                       -- JSON：二维结果表
  created_at  TEXT NOT NULL
);

-- =============================================================================
-- 八、任务编排、工具调用与日志（赛事核心考察面）
-- =============================================================================

CREATE TABLE task (
  task_id         TEXT PRIMARY KEY,
  project_id      TEXT REFERENCES project(project_id) ON DELETE CASCADE,
  user_input      TEXT NOT NULL,                   -- 用户原话
  intent          TEXT,
  skill_key       TEXT,
  plan            TEXT,                            -- JSON：步骤 + 依赖 + 工具 + 是否含 LLM
  plan_confirmed  INTEGER NOT NULL DEFAULT 0 CHECK (plan_confirmed IN (0,1)),
  status          TEXT NOT NULL
                  CHECK (status IN ('pending','planned','running','waiting_confirm',
                                    'succeeded','failed','cancelled')),
  -- 指向编排阶段那次 LLM 调用的原始记录，便于回溯「为什么这样拆步骤」
  llm_plan_call_id TEXT REFERENCES llm_call(call_id),
  error           TEXT,
  manifest_id     TEXT REFERENCES run_manifest(manifest_id),
  created_at      TEXT NOT NULL,
  started_at      TEXT,
  finished_at     TEXT
);

CREATE TABLE task_step (
  step_id     TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES task(task_id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  name        TEXT NOT NULL,
  skill_key   TEXT,
  tool_name   TEXT,
  status      TEXT NOT NULL
              CHECK (status IN ('pending','running','succeeded','failed','skipped','retrying')),
  depends_on  TEXT,                                -- JSON：step_id 列表，供前端画时间线
  input_ref   TEXT,
  output_ref  TEXT,
  error       TEXT,
  retry_of    TEXT,
  started_at  TEXT,
  finished_at TEXT
);

-- 「工具调用可完整查看」的数据源
CREATE TABLE tool_call (
  call_id         TEXT PRIMARY KEY,
  task_id         TEXT REFERENCES task(task_id) ON DELETE CASCADE,
  step_id         TEXT,
  tool_name       TEXT NOT NULL,
  tool_version    TEXT NOT NULL,
  transport       TEXT NOT NULL CHECK (transport IN ('rest','sse','mcp','cli','internal','skill')),
  -- 1 = 纯计算工具；0 = 含 LLM。前端据此区分「程序算的」与「模型理解的」。
  deterministic   INTEGER NOT NULL CHECK (deterministic IN (0,1)),
  args            TEXT NOT NULL,                   -- JSON（脱敏后）
  result_summary  TEXT NOT NULL,
  result_hash     TEXT,
  status          TEXT NOT NULL CHECK (status IN ('succeeded','failed','timeout')),
  duration_ms     INTEGER,
  error           TEXT,
  created_at      TEXT NOT NULL
);

CREATE INDEX ix_tool_call_task ON tool_call(task_id);

CREATE TABLE llm_call (
  call_id        TEXT PRIMARY KEY,
  task_id        TEXT REFERENCES task(task_id) ON DELETE CASCADE,
  step_id        TEXT,
  purpose        TEXT NOT NULL,                    -- 'intent' / 'plan' / 'claim_extract' / 'memo_text'
  model          TEXT NOT NULL,
  model_version  TEXT,
  prompt_key     TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  prompt_hash    TEXT NOT NULL,
  params         TEXT NOT NULL,                    -- temperature/seed/max_tokens，不含密钥
  input_hash     TEXT NOT NULL,
  input_digest   TEXT,
  output         TEXT,                             -- 原始输出，可回放
  tokens_in      INTEGER,
  tokens_out     INTEGER,
  latency_ms     INTEGER,
  cached         INTEGER NOT NULL DEFAULT 0 CHECK (cached IN (0,1)),
  cassette_id    TEXT,                             -- 离线回放来源
  status         TEXT NOT NULL CHECK (status IN ('succeeded','failed','timeout')),
  error          TEXT,
  created_at     TEXT NOT NULL
);

-- 多态证据指针：任何结论页只需 evidence_id 就能回到「文件 → 页码 → 原文片段」
CREATE TABLE evidence (
  evidence_id TEXT PRIMARY KEY,
  kind        TEXT NOT NULL
              CHECK (kind IN ('fact','claim','match','check','calc','llm','page','citation','peer','benchmark')),
  ref_table   TEXT NOT NULL,
  ref_id      TEXT NOT NULL,
  file_id     TEXT REFERENCES file(file_id),
  page_no     INTEGER,
  bbox        TEXT,
  quote       TEXT,
  label_cn    TEXT NOT NULL,                       -- '2024年报 p.86 合并现金流量表'
  created_at  TEXT NOT NULL
);

CREATE INDEX ix_evidence_ref ON evidence(ref_table, ref_id);

-- 文件访问审计：白名单校验 + 落库，「完整记录文件访问」的落点
CREATE TABLE file_access_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT NOT NULL,
  actor          TEXT NOT NULL,                    -- 'user:<uid>' / 'tool:<name>' / 'mcp:<server>'
  file_id        TEXT,
  rel_path       TEXT NOT NULL,
  action         TEXT NOT NULL
                 CHECK (action IN ('read','open','preview','export','deny')),
  page_no        INTEGER,
  task_id        TEXT,
  tool_call_id   TEXT,
  sha256_verified INTEGER CHECK (sha256_verified IS NULL OR sha256_verified IN (0,1)),
  allowed        INTEGER NOT NULL CHECK (allowed IN (0,1)),
  deny_reason    TEXT
);

-- 结构化应用日志。不进文件（logs 被 .gitignore 忽略），主存于此表，可查询可导出。
CREATE TABLE app_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  level      TEXT NOT NULL CHECK (level IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
  logger     TEXT NOT NULL,
  event      TEXT NOT NULL,
  request_id TEXT,
  task_id    TEXT,
  step_id    TEXT,
  message    TEXT NOT NULL,
  payload    TEXT,
  duration_ms INTEGER
);

CREATE INDEX ix_app_log_task ON app_log(task_id);
CREATE INDEX ix_app_log_level ON app_log(level, ts);

-- 「运行结果可复现」的凭据
CREATE TABLE run_manifest (
  manifest_id        TEXT PRIMARY KEY,
  project_id         TEXT REFERENCES project(project_id) ON DELETE CASCADE,
  task_id            TEXT,
  code_version       TEXT NOT NULL,
  code_dirty         INTEGER NOT NULL CHECK (code_dirty IN (0,1)),
  python_version     TEXT NOT NULL,
  deps_lock_hash     TEXT NOT NULL,
  sample_pack_version TEXT,
  model_name         TEXT,
  model_version      TEXT,
  prompt_registry_hash TEXT NOT NULL,
  rule_config_version INTEGER NOT NULL,
  random_seed        INTEGER,
  input_file_hashes  TEXT NOT NULL,                -- JSON: {file_id: sha256}
  output_hash        TEXT,
  offline_replay     INTEGER NOT NULL DEFAULT 0 CHECK (offline_replay IN (0,1)),
  created_at         TEXT NOT NULL
);

-- 人工复核留痕（诊断卡的「人工复核」按钮）
CREATE TABLE review_record (
  id           TEXT PRIMARY KEY,
  target_table TEXT NOT NULL,
  target_id    TEXT NOT NULL,
  action       TEXT NOT NULL CHECK (action IN ('confirm','reject','modify','reset_rules')),
  operator     TEXT NOT NULL,
  note         TEXT,
  created_at   TEXT NOT NULL
);

-- =============================================================================
-- 九、报告与质控
-- =============================================================================

CREATE TABLE report (
  report_id        TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  kind             TEXT NOT NULL CHECK (kind IN ('memo','audit','analysis')),
  template_key     TEXT NOT NULL,
  template_version TEXT NOT NULL,
  version          INTEGER NOT NULL,
  payload_md       TEXT NOT NULL,
  payload_json     TEXT NOT NULL,
  manifest_id      TEXT REFERENCES run_manifest(manifest_id),
  created_at       TEXT NOT NULL
);

CREATE TABLE report_citation (
  id            TEXT PRIMARY KEY,
  report_id     TEXT NOT NULL REFERENCES report(report_id) ON DELETE CASCADE,
  anchor        TEXT NOT NULL,                     -- 正文中的 [E12] 锚点
  number_ref    TEXT,                              -- 被引用的数字，供校验
  evidence_id   TEXT NOT NULL REFERENCES evidence(evidence_id),
  rendered_text TEXT NOT NULL
);

CREATE TABLE audit_finding (
  finding_id     TEXT PRIMARY KEY,
  report_id      TEXT NOT NULL REFERENCES report(report_id) ON DELETE CASCADE,
  original_text  TEXT NOT NULL,
  issue_type     TEXT NOT NULL
                 CHECK (issue_type IN ('number','unit','period','scope','multiple',
                                       'citation','opinion_mixed')),
  expected_value TEXT,
  evidence_id    TEXT REFERENCES evidence(evidence_id),
  severity       TEXT NOT NULL CHECK (severity IN ('info','warn','error')),
  fix_suggestion TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('confirmed','needs_review','ignored')),
  created_at     TEXT NOT NULL
);

-- 评测页数据
CREATE TABLE dataset_eval (
  eval_id        TEXT PRIMARY KEY,
  eval_name      TEXT NOT NULL,
  dataset_version TEXT NOT NULL,
  metric         TEXT NOT NULL,
  value          TEXT NOT NULL,
  baseline_value TEXT,
  detail         TEXT,
  created_at     TEXT NOT NULL
);

-- =============================================================================
-- 十、视图：让不可信数据在 SQL 层就进不来
-- =============================================================================

-- 所有进入指数与估值的查询只允许读这个视图。
-- needs_review / not_found / rejected / 不可比的行在此被挡在门外。
CREATE VIEW v_fact_verified AS
SELECT *
FROM financial_fact
WHERE status = 'validated'
  AND comparable = 1;

-- 「未找到」由视图生成，绝不落成 value = 0 的行。
-- 指标 × 期间的完整网格左连接事实表，缺失处 status 显示为 'not_found'。
--
-- 注意 CAST：json_each 对 '[2022,2023,2024]' 返回的是 INTEGER，而 financial_fact.period
-- 是 TEXT。SQLite 比较时不跨类型隐式转换（'2024' = 2024 为假），不 CAST 的话
-- LEFT JOIN 永远匹配不上，所有有数据的期间都会被误报成 not_found。
CREATE VIEW v_fact_grid AS
SELECT
  p.project_id,
  md.metric_key,
  md.label_cn,
  CAST(fy.value AS TEXT) AS period,
  f.fact_id,
  f.value_millions,
  f.raw_unit,
  f.unit,
  f.scope,
  f.source_file,
  f.source_page,
  f.source_printed_page,
  f.confidence,
  COALESCE(f.status, 'not_found') AS status,
  COALESCE(f.comparable, 1) AS comparable,
  f.incomparable_reason
FROM project p
CROSS JOIN metric_definition md
CROSS JOIN json_each(p.fiscal_years) fy
LEFT JOIN financial_fact f
  ON  f.project_id = p.project_id
  AND f.metric_key = md.metric_key
  AND f.period     = CAST(fy.value AS TEXT)
  AND f.scope      = p.base_scope
  AND f.is_primary = 1;

-- 诊断指数只读已验证事实
CREATE VIEW v_match_evidence AS
SELECT
  m.match_id,
  m.claim_id,
  m.metric_key,
  m.verdict,
  m.reason,
  m.confidence,
  c.claim_text,
  c.source_page  AS claim_page,
  c.source_file_id AS claim_file_id,
  f.fact_id,
  f.value_millions,
  f.source_page  AS fact_page,
  f.source_file  AS fact_source_file
FROM claim_match m
JOIN claim c ON c.claim_id = m.claim_id
LEFT JOIN financial_fact f
  ON f.fact_id = m.fact_id
 AND f.status = 'validated'
 AND f.comparable = 1;
