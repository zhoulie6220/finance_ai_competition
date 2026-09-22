"""数据契约的枚举定义。

与 `app/db/schema.sql` 里的 CHECK 约束**逐一对应**。改动任意一侧都必须同步另一侧，
否则会出现「Pydantic 放行、数据库拒绝」或更糟的「数据库放行、语义已变」。

用 StrEnum 而非 Enum：序列化成 JSON 时直接是字符串，前端无需额外转换。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

E = TypeVar("E", bound=type)


def _with_cn_title(
    cls: Any, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
) -> JsonSchemaValue:
    js = handler(schema)
    title = getattr(cls, "title_cn", "")
    if title:
        js["title"] = title
    return js


def cn_enum(title: str) -> Callable[[E], E]:
    """给枚举在 OpenAPI 里挂一个中文标题。

    枚举的 title 默认取**类名**（`FileRole`），而类名是英文——`/docs` 的 Schemas 区
    和前端从 `contract.json` 生成的类型名都会照搬它。Pydantic 认 `model_config` 的只有
    BaseModel，Enum 没有这条路，所以这里在类创建后挂一个 JSON Schema 钩子。

    ⚠ 不要改成「让枚举继承一个带 title 的基类」：Python 的 Enum 不许被有成员的枚举
      继承，而用 `ClassVar` / `nonmember` 声明基类属性也挡不住子类里的赋值——
      赋值照样会变成**一个真实的枚举成员**，于是 `list(FileRole)` 里凭空多出
      `title_cn`，所有遍历枚举的地方都会多跑一轮。这个坑实测过。

    **取值本身（'annual_report' 等）不翻译**——它们是要落库、要进 JSON、要与
    schema.sql 的 CHECK 逐字对上的数据，不是文案。要说明取值含义就写进类 docstring。
    """
    def decorate(cls: E) -> E:
        cls.title_cn = title
        cls.__get_pydantic_json_schema__ = classmethod(_with_cn_title)
        return cls

    return decorate


@cn_enum("会计口径")
class Scope(StrEnum):
    """会计口径。默认合并口径；母公司口径只在用户明确选择时展示。"""

    CONSOLIDATED = "consolidated"
    PARENT = "parent"


@cn_enum("数值期间性质")
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


@cn_enum("财务事实状态")
class FactStatus(StrEnum):
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    # 由 v_fact_grid 视图生成，从不落库
    NOT_FOUND = "not_found"
    REJECTED = "rejected"


@cn_enum("不可比原因")
class IncomparableReason(StrEnum):
    """不可直接比较的原因。标记后不进指数扣分，但从主中枢中排除。

    ⚠ 取值必须与 `schema.sql` 里 `financial_fact` 和 `normalization_year` 两张表的
      `incomparable_reason` CHECK 完全一致。这三处曾经分叉过：Pydantic 放行
      `asset_injection`，而 `financial_fact` 的 CHECK 里没有它——于是签字文档 §4
      明确要求标记的「资产注入年度」**写不进去**，报的还是一句 constraint failed。
      `tests/unit/schemas/test_contract.py` 里有对拍测试盯着这三处。
    """

    MNA = "mna"
    RESTRUCTURING = "restructuring"
    ASSET_INJECTION = "asset_injection"
    SCOPE_CHANGE = "scope_change"
    RESTATEMENT = "restatement"
    POLICY_CHANGE = "policy_change"
    INDUSTRY_CYCLE = "industry_cycle"
    SEASONALITY = "seasonality"
    OTHER = "other"


@cn_enum("规则参数分级")
class ParamTier(StrEnum):
    """规则参数的分级（签字文档 A-8）。

    决定改一个参数需要谁点头，而不是它是「重要」还是「不重要」——
    `soft` 的提示语照样可以改错，只是改错不会影响任何数字。
    """

    HARD = "hard"      # 字段口径、EBIT、周期窗口、阈值、减值、估值公式 → 发布前会计签字
    SOFT = "soft"      # 提示语、颜色、排序 → 可后续调整
    MODEL = "model"    # 模型名、Prompt 版本、温度、输出长度 → 记版本，不属于会计口径


@cn_enum("报表类型")
class Statement(StrEnum):
    BALANCE = "balance"
    INCOME = "income"
    CASHFLOW = "cashflow"
    INDICATOR = "indicator"
    INDUSTRY = "industry"
    # 披露事项：审计意见、会计政策变更这类没有数值的抽取目标
    DISCLOSURE = "disclosure"


@cn_enum("数值类型")
class ValueType(StrEnum):
    STOCK = "stock"      # 时点（资产负债表）
    FLOW = "flow"        # 期间（利润表、现金流量表）
    RATIO = "ratio"      # 比率
    TEXT = "text"        # 文本，无对应数值


@cn_enum("单位类型")
class UnitKind(StrEnum):
    CURRENCY = "currency"
    PERCENT = "percent"
    SHARES = "shares"
    DAYS = "days"
    TON = "ton"
    QUANTITY = "quantity"
    TEXT = "text"


@cn_enum("例句来源")
class ExampleSource(StrEnum):
    """字段字典里例句的来源。

    `synthetic_example` 是带【数值】占位符的标准句，只供解析器回归测试用，
    **不得**在界面或报告里当作年报原文展示。
    """

    ANNUAL_REPORT = "annual_report"
    SYNTHETIC_EXAMPLE = "synthetic_example"


@cn_enum("正负号含义")
class SignConvention(StrEnum):
    POSITIVE_IS_GOOD = "positive_is_good"
    NEGATIVE_IS_GOOD = "negative_is_good"
    NEUTRAL = "neutral"


@cn_enum("数据在年报中的位置")
class SourceLocation(StrEnum):
    """同一数字在年报中的位置。位置不同可信度不同，交叉校验时按此加权。"""

    MAIN_STATEMENT = "main_statement"      # 三张主表，最可信
    INDICATOR_TABLE = "indicator_table"    # 主要会计数据和财务指标
    NOTES = "notes"                        # 附注
    MDNA_TEXT = "mdna_text"                # 正文叙述，常带「约」「近」等模糊表述
    OTHER = "other"


@cn_enum("多源观测裁决")
class ObservationResolution(StrEnum):
    """同一次取值的多个观测，裁决结果。"""

    PENDING = "pending"
    ADOPTED = "adopted"
    REJECTED = "rejected"


@cn_enum("校验状态")
class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    # 缺数据导致无法校验——**不等于通过**
    SKIPPED_MISSING_DATA = "skipped_missing_data"
    SKIPPED_INCOMPARABLE = "skipped_incomparable"


@cn_enum("严重程度")
class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@cn_enum("主张方向")
class ClaimDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    IMPROVE = "improve"
    DETERIORATE = "deteriorate"
    FLAT = "flat"
    UNKNOWN = "unknown"


@cn_enum("主张主题")
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


@cn_enum("主张—事实匹配结论")
class MatchVerdict(StrEnum):
    """主张—事实匹配的四态 + 部分支持。

    `partial` 用于「方向一致但幅度明显偏弱」，避免把只兑现一半与完全兑现混为一谈。
    """

    SUPPORTED = "supported"
    PARTIAL = "partial"
    CONFLICTED = "conflicted"
    INCOMPARABLE = "incomparable"   # 不进分母、不扣分
    MISSING = "missing"             # 不推断为失败，转人工复核


@cn_enum("诊断指数等级")
class IndexGrade(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    # 覆盖率或观测数不足时不出分
    INSUFFICIENT = "insufficient"


@cn_enum("周期正常化状态")
class NormalizationStatus(StrEnum):
    NORMALIZED = "normalized"                                 # 主窗口 8 年成功
    EXTENDED_NORMALIZED = "extended_normalized"               # 回退窗口 10 年成功
    INSUFFICIENT_DATA = "NORMALIZATION_INSUFFICIENT_DATA"     # 可比年度不足
    INCOMPLETE_CYCLE = "incomplete_cycle"                     # 周期覆盖不完整


@cn_enum("正常化窗口模式")
class WindowMode(StrEnum):
    PRIMARY_8Y = "primary_8y"
    FALLBACK_10Y = "fallback_10y"


@cn_enum("EBIT 口径")
class EbitVariant(StrEnum):
    """Reported 为 DCF 默认；Adjusted 需会计逐笔批准后才可切换。"""

    REPORTED = "reported"
    ADJUSTED = "adjusted"


@cn_enum("周期位置")
class CyclePosition(StrEnum):
    """当前处于周期什么位置。必须显式标注，不能假装周期不存在。"""

    PEAK = "peak"
    ABOVE_MID = "above_mid"
    MID = "mid"
    BELOW_MID = "below_mid"
    TROUGH = "trough"
    UNKNOWN = "unknown"


@cn_enum("EBIT 调整类别")
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


@cn_enum("调整方向")
class AdjustmentDirection(StrEnum):
    ADD_BACK = "add_back"
    DEDUCT = "deduct"


@cn_enum("调整复核状态")
class AdjustmentReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@cn_enum("估值参数来源")
class ParamSourceType(StrEnum):
    """估值参数的来源。禁止模型凭记忆填入。"""

    HISTORICAL_FACT = "historical_fact"
    USER_INPUT = "user_input"
    COMPARABLE_STAT = "comparable_stat"
    MODEL_ASSUMPTION = "model_assumption"


@cn_enum("盈利状态")
class Profitability(StrEnum):
    PROFITABLE = "profitable"
    LOSS = "loss"          # 亏损公司不得机械使用 PE


@cn_enum("可比公司角色")
class PeerRole(StrEnum):
    """可比公司的角色。

    `chain_reference`（产业链上游参照）**不得进入估值可比集**——
    煤价是钢企的成本项，两者周期驱动因素相反，混进中位数会让它失去意义。
    """

    VALUATION_PEER = "valuation_peer"
    CHAIN_REFERENCE = "chain_reference"


@cn_enum("文件角色")
class FileRole(StrEnum):
    ANNUAL_REPORT = "annual_report"
    HALF_YEAR = "half_year"
    QUARTERLY = "quarterly"
    ANNOUNCEMENT = "announcement"
    COMPARABLE = "comparable"
    RESEARCH_DRAFT = "research_draft"
    INDUSTRY_DATA = "industry_data"


@cn_enum("解析状态")
class ParseStatus(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    PARSED = "parsed"
    FAILED = "failed"


@cn_enum("MD&A 章节类型")
class MdnaSectionKind(StrEnum):
    MDNA = "mdna"
    RISK_DISCLOSURE = "risk_disclosure"
    BUSINESS_REVIEW = "business_review"
    OUTLOOK = "outlook"
    OTHER = "other"


@cn_enum("任务状态")
class TaskStatus(StrEnum):
    PENDING = "pending"
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_CONFIRM = "waiting_confirm"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@cn_enum("步骤状态")
class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


@cn_enum("工具调用通道")
class ToolTransport(StrEnum):
    REST = "rest"
    SSE = "sse"
    MCP = "mcp"
    CLI = "cli"
    INTERNAL = "internal"
    SKILL = "skill"


@cn_enum("证据类型")
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


@cn_enum("研报问题类型")
class AuditIssueType(StrEnum):
    NUMBER = "number"
    UNIT = "unit"
    PERIOD = "period"
    SCOPE = "scope"
    MULTIPLE = "multiple"
    CITATION = "citation"
    OPINION_MIXED = "opinion_mixed"     # 事实与观点混写


@cn_enum("研报问题状态")
class AuditFindingStatus(StrEnum):
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    IGNORED = "ignored"


@cn_enum("人工复核动作")
class ReviewAction(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    MODIFY = "modify"
    RESET_RULES = "reset_rules"


@cn_enum("报告类型")
class ReportKind(StrEnum):
    MEMO = "memo"
    AUDIT = "audit"
    ANALYSIS = "analysis"


@cn_enum("日志级别")
class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
