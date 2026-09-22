"""数据契约的唯一定义源（Pydantic v2）。

三个人在这里交汇：后端按它落库、引擎按它返回、前端按它渲染。
`docs/01-data-contract.md` 是人类可读版本，本包是机读版本，两者必须同步。

用法：

    from app.schemas import FinancialFact, NormalizationRun, MatchVerdict

导出 JSON Schema 给前端：

    python scripts/export_schemas.py
"""

from app.schemas.api import (
    DatabaseStatus,
    EventHistoryResponse,
    FileListResponse,
    FileView,
    HealthResponse,
    MetaResponse,
    ProjectListResponse,
    ProjectView,
    SkillListResponse,
    SkillView,
    TaskListResponse,
    TaskResponse,
    TaskStepView,
    TaskSummary,
    ToolCallListResponse,
    ToolListResponse,
    ToolSpecView,
    ValidationErrorResponse,
    ValidationIssue,
)
from app.schemas.claim import Claim, ClaimIndicator, ClaimMatch, MdnaSection
from app.schemas.diagnosis import (
    DiagnosisComponent,
    DiagnosisRun,
    RuleConfigItem,
    ScenarioDelta,
)
from app.schemas.enums import (
    AdjustmentCategory,
    AdjustmentDirection,
    AdjustmentReviewStatus,
    AuditFindingStatus,
    AuditIssueType,
    ClaimDirection,
    ClaimType,
    CheckStatus,
    CyclePosition,
    EbitVariant,
    EvidenceKind,
    FactStatus,
    FileRole,
    IncomparableReason,
    IndexGrade,
    LogLevel,
    MatchVerdict,
    MdnaSectionKind,
    NormalizationStatus,
    ObservationResolution,
    ParamSourceType,
    ParseStatus,
    PeerRole,
    PeriodKind,
    Profitability,
    Scope,
    Severity,
    SignConvention,
    SourceLocation,
    Statement,
    StepStatus,
    TaskStatus,
    ToolTransport,
    UnitKind,
    ValueType,
    WindowMode,
)
from app.schemas.evidence import (
    MEMO_SECTIONS,
    AuditFinding,
    Evidence,
    Report,
    ReportCitation,
)
from app.schemas.fact import (
    CheckResult,
    FactCorrection,
    FactObservation,
    FinancialFact,
    MetricDefinition,
)
from app.schemas.task import (
    AppLog,
    FileAccessLog,
    LlmCall,
    RunManifest,
    Task,
    TaskEvent,
    TaskStep,
    ToolCall,
)
from app.schemas.types import Money, Ratio
from app.schemas.valuation import (
    ComparableCompany,
    EbitAdjustment,
    NormalizationCrosscheck,
    NormalizationRun,
    NormalizationYear,
    ValuationParam,
    ValuationRun,
    ValuationScenario,
)

__all__ = [
    # 枚举
    "AdjustmentCategory", "AdjustmentDirection", "AdjustmentReviewStatus",
    "AuditFindingStatus", "AuditIssueType", "CheckStatus", "ClaimDirection",
    "ClaimType", "CyclePosition", "EbitVariant", "EvidenceKind", "FactStatus",
    "FileRole", "IncomparableReason", "IndexGrade", "LogLevel", "MatchVerdict",
    "MdnaSectionKind", "NormalizationStatus", "ObservationResolution",
    "ParamSourceType", "ParseStatus", "PeerRole", "PeriodKind", "Profitability",
    "Scope", "Severity", "SignConvention", "SourceLocation", "Statement",
    "StepStatus", "TaskStatus", "ToolTransport", "UnitKind", "ValueType",
    "WindowMode",
    # 标量
    "Money", "Ratio",
    # 事实
    "FinancialFact", "FactObservation", "FactCorrection", "CheckResult",
    "MetricDefinition",
    # 主张
    "MdnaSection", "Claim", "ClaimIndicator", "ClaimMatch",
    # 诊断
    "DiagnosisRun", "DiagnosisComponent", "RuleConfigItem", "ScenarioDelta",
    # 估值
    "NormalizationRun", "NormalizationYear", "EbitAdjustment",
    "NormalizationCrosscheck", "ComparableCompany", "ValuationRun",
    "ValuationScenario", "ValuationParam",
    # 任务与日志
    "Task", "TaskStep", "ToolCall", "LlmCall", "FileAccessLog", "RunManifest",
    "AppLog", "TaskEvent",
    # 证据与报告
    "Evidence", "Report", "ReportCitation", "AuditFinding", "MEMO_SECTIONS",
    # HTTP 接口形状（响应模型；与业务实体分开，见 app/schemas/api.py）
    "TaskSummary", "TaskStepView", "TaskResponse", "TaskListResponse",
    "ToolCallListResponse", "EventHistoryResponse", "ValidationIssue",
    "ValidationErrorResponse", "HealthResponse", "DatabaseStatus",
    "ToolSpecView", "ToolListResponse", "SkillView", "SkillListResponse",
    "MetaResponse", "ProjectView", "ProjectListResponse", "FileView",
    "FileListResponse",
]
