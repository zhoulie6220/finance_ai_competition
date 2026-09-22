"""证据链、报告与质控。

「点击任意结论至少能回到一个原始来源」这条验收标准的实现基础：
`Evidence` 是一张多态指针表，任何结论只需一个 evidence_id 就能解析到
「文件 → 页码 → 原文片段」。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import AuditFindingStatus, AuditIssueType, EvidenceKind, ReportKind, Severity


class Evidence(BaseModel):
    """一条证据指针。

    `ref_table` + `ref_id` 指向系统里任何一个可被引用的对象（事实、主张、匹配、
    校验、计算、页面……），`file_id` + `page_no` + `quote` 指向它在年报里的出处。
    """

    model_config = ConfigDict(title="证据指针", from_attributes=True)

    evidence_id: str
    kind: EvidenceKind
    ref_table: str
    ref_id: str
    file_id: str | None = None
    page_no: int | None = None
    bbox: str | None = None
    quote: str | None = None
    label_cn: str = Field(description="给用户看的出处描述，如「2024年报 p.86 合并现金流量表」")
    created_at: str


class ReportCitation(BaseModel):
    """报告正文里的一个引用锚点。

    `number_ref` 记录被引用的数字，用于校验「正文写的数」与「证据里的数」是否一致——
    这是防止模型在组织文字时悄悄改数的最后一道检查。
    """

    model_config = ConfigDict(title="报告引用锚点", from_attributes=True)

    id: str
    report_id: str
    anchor: str = Field(description="正文中的锚点，如 '[E12]'")
    number_ref: str | None = None
    evidence_id: str
    rendered_text: str


class AuditFinding(BaseModel):
    """研究报告纠错的一条发现。"""

    model_config = ConfigDict(title="研报纠错发现", from_attributes=True)

    finding_id: str
    report_id: str
    original_text: str
    issue_type: AuditIssueType
    expected_value: str | None = None
    evidence_id: str | None = None
    severity: Severity
    fix_suggestion: str
    status: AuditFindingStatus = AuditFindingStatus.NEEDS_REVIEW
    created_at: str


class Report(BaseModel):
    """一份生成的报告（备忘录 / 纠错清单 / 分析）。

    备忘录使用固定九段模板，**不让模型自由发挥结构**——结构一自由，风险与证伪
    条件这类「不写也没人发现」的段落就会消失。
    """

    model_config = ConfigDict(title="生成报告", from_attributes=True)

    report_id: str
    project_id: str
    kind: ReportKind
    template_key: str
    template_version: str
    version: int
    payload_md: str
    payload_json: dict = Field(default_factory=dict)
    manifest_id: str | None = None
    citations: list[ReportCitation] = Field(default_factory=list)
    created_at: str


#: 买方投资备忘录的固定结构。顺序即模板顺序，不得增删。
MEMO_SECTIONS: tuple[str, ...] = (
    "公司与研究范围",
    "投资结论摘要",
    "财务事实与趋势",
    "MD&A 主张—事实一致性",
    "估值区间与关键假设",
    "支持证据与反方论据",
    "风险因素",
    "证伪条件与持续跟踪指标",
    "数据来源与免责声明",
)
