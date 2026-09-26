"""数据契约的枚举定义。

与 `app/db/schema.sql` 里的 CHECK 约束**逐一对应**。改动任意一侧都必须同步另一侧，
否则会出现「Pydantic 放行、数据库拒绝」或更糟的「数据库放行、语义已变」。

用 StrEnum 而非 Enum：序列化成 JSON 时直接是字符串，前端无需额外转换。
"""

from __future__ import annotations

from enum import StrEnum


class Scope(StrEnum):
    """会计口径。默认合并口径；母公司口径只在用户明确选择时展示。"""

    CONSOLIDATED = "consolidated"
    PARENT = "parent"


class PeriodKind(StrEnum):
    """数值本身的性质，不是它出现在哪份报告里。

    刻意不含 'prior'：FY2023 的数字无论在 2023 年报的「本期」栏还是 2024 年报的
    「上期」栏，都是同一笔事实，period='2023' + kind='current'。多一个 kind 会让
    同一笔事实存成两行，聚合时重复计算。
    """

    CURRENT = "current"
    INSTANT = "instant"
    OPENING = "opening"
    AVERAGE = "average"


class FactStatus(StrEnum):
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    # 由 v_fact_grid 视图生成，从不落库
    NOT_FOUND = "not_found"
    REJECTED = "rejected"


class IncomparableReason(StrEnum):
    """不可直接比较的原因。标记后不进指数扣分，但从主中枢中排除。"""

    MNA = "mna"
    RESTRUCTURING = "restructuring"
    ASSET_INJECTION = "asset_injection"
    SCOPE_CHANGE = "scope_change"
    RESTATEMENT = "restatement"
    POLICY_CHANGE = "policy_change"
    INDUSTRY_CYCLE = "industry_cycle"
    OTHER = "other"


class Statement(StrEnum):
    BALANCE = "balance"
    INCOME = "income"
    CASHFLOW = "cashflow"
    INDICATOR = "indicator"
    INDUSTRY = "industry"
    # 披露事项：审计意见、会计政策变更这类没有数值的抽取目标
    DISCLOSURE = "disclosure"


class ValueType(StrEnum):
    STOCK = "stock"      # 时点（资产负债表）
    FLOW = "flow"        # 期间（利润表、现金流量表）
    RATIO = "ratio"      # 比率
    TEXT = "text"        # 文本，无对应数值


class UnitKind(StrEnum):
    CURRENCY = "currency"
    PERCENT = "percent"
    SHARES = "shares"
    DAYS = "days"
    TON = "ton"
    QUANTITY = "quantity"
    TEXT = "text"


class ExampleSource(StrEnum):
    """字段字典里例句的来源。

    `synthetic_example` 是带【数值】占位符的标准句，只供解析器回归测试用，
    **不得**在界面或报告里当作年报原文展示。
    """

    ANNUAL_REPORT = "annual_report"
    SYNTHETIC_EXAMPLE = "synthetic_example"


class SignConvention(StrEnum):
    """指标的经济方向：数值越大是好事还是坏事。

    ⚠ 它描述的是**经济方向**，不是**列报符号**——资产减值类指标两者都要：
    经济方向是「损失越大越坏」，而列报符号要看那一年年报是正数列示还是
    「损失以负号填列」。后者记在 financial_fact.sign_basis 上，不是这里。
    """

    POSITIVE_IS_GOOD = "positive_is_good"
    NEGATIVE_IS_GOOD = "negative_is_good"
    NEUTRAL = "neutral"
    # 会计口径 v1.1 §二：资产减值损失的标准化列报为**损失额为正**。
    # 2015 年正数列示 1,486,729,666.32 → 不变；
    # 2024 年「损失以负号填列」-578,764,052.76 → 取反为 +578,764,052.76。
    # 禁止用绝对值：允许转回的项目在新版列报里为正时，标准化应为负（表示净收益）。
    LOSS_POSITIVE = "loss_positive"


class SourceLocation(StrEnum):
    """同一数字在年报中的位置。位置不同可信度不同，交叉校验时按此加权。"""

    MAIN_STATEMENT = "main_statement"      # 三张主表，最可信
    INDICATOR_TABLE = "indicator_table"    # 主要会计数据和财务指标
    NOTES = "notes"                        # 附注
    MDNA_TEXT = "mdna_text"                # 正文叙述，常带「约」「近」等模糊表述
    OTHER = "other"


class ObservationResolution(StrEnum):
    """同一次取值的多个观测，裁决结果。"""

    PENDING = "pending"
    ADOPTED = "adopted"
    REJECTED = "rejected"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    # 缺数据导致无法校验——**不等于通过**
    SKIPPED_MISSING_DATA = "skipped_missing_data"
    SKIPPED_INCOMPARABLE = "skipped_incomparable"


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class ClaimDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    IMPROVE = "improve"
    DETERIORATE = "deteriorate"
    FLAT = "flat"
    UNKNOWN = "unknown"


class ClaimType(StrEnum):
    """MD&A 主张的主题。对应大框架里的四类叙事信号。"""

    DEMAND = "demand"
    ORDER = "order"
    CAPACITY = "capacity"
    COLLECTION = "collection"
    PRODUCT_MIX = "product_mix"
    COST = "cost"
    RISK = "risk"
    MACRO = "macro"
    OTHER = "other"


class MatchVerdict(StrEnum):
    """主张—事实匹配的判定。会计口径 v1.1 §A.2 的 s 标度。

    三个**计分**态，构成 H / C 的等权平均：

        supported     s = +1   同口径直接证据满足目标，或实质支持主张方向
        neutral       s =  0   方向性主张对应的变化落在噪声区间内
        contradicted  s = -1   直接证据超过阈值且方向相反，或明确数值目标未达成

    三个**不计分**态，留在页面单列，理由必须写进 `reason`：

        needs_review  —— 只有间接代理、口径不一致或来源冲突，尚未核清。**留在覆盖率分母**
        unverifiable  —— 未披露直接指标，或主张目标不够具体。**不进分母**
        incomparable  —— 并购/重组/政策变更/重述/周期/季节性导致不可比。不进分母、不扣分

    ⚠ v1.1 明确删掉了旧稿的 `partial`（方向一致但幅度偏弱），并把 H、C 的取值域
    从 [0,1] 改成 [-1,1]。旧定义下五个分项都是 0–1，中性证据只能取 0.5，于是
    H=C=0.5 会算出 50 + 20×0.5 + 20×0.5 = 70 —— **正好压在「一致性较高」的分界线上**。
    改成 [-1,1] 之后全中性得 50 分，才是诚实的。
    """

    SUPPORTED = "supported"
    NEUTRAL = "neutral"
    CONTRADICTED = "contradicted"
    NEEDS_REVIEW = "needs_review"
    UNVERIFIABLE = "unverifiable"
    INCOMPARABLE = "incomparable"


class IndexGrade(StrEnum):
    """分级线见 rule_config：index.grade_high_min=70、index.grade_low_max=45。

    按**未四舍五入**的值分级。取 insufficient_evidence 时 `score` 必须为 NULL ——
    绝不用 0 分或 50 分代替：页面一定会把那个数字渲染成一个大号分数，
    人工复核的入口就形同虚设了。
    """

    HIGH = "high"                            # I >= 70
    MEDIUM = "medium"                        # 45 <= I < 70
    LOW = "low"                              # I < 45
    INSUFFICIENT = "insufficient_evidence"   # 闸门四条件未全过，不出分


class NormalizationStatus(StrEnum):
    NORMALIZED = "normalized"                                 # 主窗口 8 年成功
    EXTENDED_NORMALIZED = "extended_normalized"               # 回退窗口 10 年成功
    INSUFFICIENT_DATA = "NORMALIZATION_INSUFFICIENT_DATA"     # 可比年度不足
    INCOMPLETE_CYCLE = "incomplete_cycle"                     # 周期覆盖不完整


class WindowMode(StrEnum):
    PRIMARY_8Y = "primary_8y"
    FALLBACK_10Y = "fallback_10y"


class EbitVariant(StrEnum):
    """Reported 为 DCF 默认；Adjusted 需会计逐笔批准后才可切换。"""

    REPORTED = "reported"
    ADJUSTED = "adjusted"


class CyclePosition(StrEnum):
    """当前处于周期什么位置。必须显式标注，不能假装周期不存在。"""

    PEAK = "peak"
    ABOVE_MID = "above_mid"
    MID = "mid"
    BELOW_MID = "below_mid"
    TROUGH = "trough"
    UNKNOWN = "unknown"


class AdjustmentCategory(StrEnum):
    GOV_SUBSIDY = "gov_subsidy"
    ASSET_DISPOSAL = "asset_disposal"
    INVESTMENT_INCOME = "investment_income"
    FAIR_VALUE_CHANGE = "fair_value_change"
    HEDGING = "hedging"
    IMPAIRMENT = "impairment"
    RESTRUCTURING = "restructuring"
    RELATED_PARTY = "related_party"
    OTHER = "other"


class AdjustmentDirection(StrEnum):
    ADD_BACK = "add_back"
    DEDUCT = "deduct"


class AdjustmentReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ParamSourceType(StrEnum):
    """估值参数的来源。禁止模型凭记忆填入。"""

    HISTORICAL_FACT = "historical_fact"
    USER_INPUT = "user_input"
    COMPARABLE_STAT = "comparable_stat"
    MODEL_ASSUMPTION = "model_assumption"


class Profitability(StrEnum):
    PROFITABLE = "profitable"
    LOSS = "loss"          # 亏损公司不得机械使用 PE


class PeerRole(StrEnum):
    """可比公司的角色。

    `chain_reference`（产业链上游参照）**不得进入估值可比集**——
    煤价是钢企的成本项，两者周期驱动因素相反，混进中位数会让它失去意义。
    """

    VALUATION_PEER = "valuation_peer"
    CHAIN_REFERENCE = "chain_reference"


class FileRole(StrEnum):
    ANNUAL_REPORT = "annual_report"
    HALF_YEAR = "half_year"
    QUARTERLY = "quarterly"
    ANNOUNCEMENT = "announcement"
    COMPARABLE = "comparable"
    RESEARCH_DRAFT = "research_draft"
    INDUSTRY_DATA = "industry_data"


class ParseStatus(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    PARSED = "parsed"
    FAILED = "failed"


class MdnaSectionKind(StrEnum):
    MDNA = "mdna"
    RISK_DISCLOSURE = "risk_disclosure"
    BUSINESS_REVIEW = "business_review"
    OUTLOOK = "outlook"
    OTHER = "other"


class TaskStatus(StrEnum):
    PENDING = "pending"
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_CONFIRM = "waiting_confirm"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


class ToolTransport(StrEnum):
    REST = "rest"
    SSE = "sse"
    MCP = "mcp"
    CLI = "cli"
    INTERNAL = "internal"
    SKILL = "skill"


class EvidenceKind(StrEnum):
    FACT = "fact"
    CLAIM = "claim"
    MATCH = "match"
    CHECK = "check"
    CALC = "calc"
    LLM = "llm"
    PAGE = "page"
    CITATION = "citation"
    PEER = "peer"
    BENCHMARK = "benchmark"


class AuditIssueType(StrEnum):
    NUMBER = "number"
    UNIT = "unit"
    PERIOD = "period"
    SCOPE = "scope"
    MULTIPLE = "multiple"
    CITATION = "citation"
    OPINION_MIXED = "opinion_mixed"     # 事实与观点混写


class AuditFindingStatus(StrEnum):
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    IGNORED = "ignored"


class ReviewAction(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    MODIFY = "modify"
    RESET_RULES = "reset_rules"


class ReportKind(StrEnum):
    MEMO = "memo"
    AUDIT = "audit"
    ANALYSIS = "analysis"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
