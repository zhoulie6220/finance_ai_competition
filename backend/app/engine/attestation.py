"""需要**人工逐项确认**的两个分项：R（风险披露充分度）与 P（缺乏可验证性）。

纯函数，零 IO。口径见会计口径 gap_closure_v1.0 §三、§四。

## 为什么放在一起

它们的共同点不是算法，是**都不能由程序单独定**：

· R 要「明确风险对象 + 说明作用路径 + 给出可核验证据」三项齐全才算 1 分，
  而「作用路径是否说清楚了」是判断题。
  会计口径点名禁止用风险词频代替：「披露充分不代表风险小」。
· P 会计口径原话：「模型只能提出候选，**不能自动定P**」。

所以这两个模块算的不是数据，是**人工判断的汇总**。

## 共同的坑：把「没核完」当成「没问题」

两个分项的分母都必须是「**已完成核验**且适用的项数」。把没核完的
也算进分母、或者当成「未触发」，会让 R、P 偏低——而指数会**偏高**。
而且不报错。

所以：
· `verified=False` 时，组件值算得出来也不作数——**闸门会拦住**
· 未核验的项**不进分母**，但会让组件标记为「不完整」
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.engine.index import RatioComponent

#: R 的四项固定检查（会计口径 §3.2）。**不许增减**——
#: 加一项少一项都会让 R 的分母变，而分母变了比例就没法比。
RISK_ITEMS: tuple[tuple[str, str], ...] = (
    ("demand_price", "需求与钢价"),
    ("fuel_cost", "原燃料成本"),
    ("environment_capacity", "环保与产能"),
    ("liquidity_collection", "流动性与回款"),
)

ITEM_LABELS = dict(RISK_ITEMS)

RiskConclusion = Literal[
    "pending", "sufficient", "insufficient", "not_applicable", "needs_review"
]
PConclusion = Literal["pending", "p_penalty", "no_penalty", "needs_review"]


# ---------------------------------------------------------------- R


@dataclass(frozen=True)
class RiskItemResult:
    """一项风险检查的录入内容（对应 risk_disclosure_check 表的一行）。"""

    item: str
    applicable: bool
    conclusion: RiskConclusion
    risk_object: str | None = None
    impact_path: str | None = None
    evidence: str | None = None
    reason: str | None = None
    #: 这一格是哪个年度的。R 的一格 = **一个年度 × 一个固定项**，
    #: 与导出给会计的 `R_风险检查.csv` 同粒度（宝钢 10 年 × 4 项 = 40 格）。
    period: str | None = None

    @property
    def label(self) -> str:
        base = ITEM_LABELS.get(self.item, self.item)
        return f"{base}（{self.period}）" if self.period else base

    @property
    def has_three_elements(self) -> bool:
        """三个证据要素是否齐全。"""
        return all(
            v is not None and v.strip()
            for v in (self.risk_object, self.impact_path, self.evidence)
        )

    @property
    def resolved(self) -> bool:
        """这一项有没有拿到**明确结论**。

        `pending`（还没填）与 `needs_review`（有冲突待查）都算没有——
        「查了没问题」和「还没查」必须分得开。
        """
        return self.conclusion in ("sufficient", "insufficient")


def risk_component(items: list[RiskItemResult]) -> RatioComponent:
    """R = 证据三要素齐全的项数 / 已完成核验且适用的项数。

    ⚠ 「不适用」必须附业务范围证据——会计口径 §3.3 明确：
    「缺少风险章节不是『不适用』，而是 insufficient」。
    所以 `applicable=False` 但没有证据的项，**不进分母也不作数**，
    而是让整个组件变成「未核验」。
    """
    applicable = [i for i in items if i.applicable]
    not_applicable_without_evidence = [
        i for i in items if not i.applicable and not (i.evidence or "").strip()
    ]

    resolved = [i for i in applicable if i.resolved]
    sufficient = [i for i in resolved if i.conclusion == "sufficient"]

    unresolved = [i for i in applicable if not i.resolved]
    problems: list[str] = []
    if unresolved:
        problems.append(
            "未核验：" + "、".join(f"{i.label}（{i.conclusion}）" for i in unresolved)
        )
    if not_applicable_without_evidence:
        problems.append(
            "标了不适用但没写业务范围证据："
            + "、".join(i.label for i in not_applicable_without_evidence)
        )

    return RatioComponent(
        name="risk_shift",
        label_cn="风险披露充分度 R",
        numerator=len(sufficient),
        denominator=len(resolved),
        verified=not problems and bool(resolved),
        note="；".join(problems),
    )


# ---------------------------------------------------------------- P


@dataclass(frozen=True)
class PConfirmation:
    """一条主张的人工确认结果（对应 p_confirmation 表的一行）。"""

    claim_id: str
    is_substantive: bool
    conclusion: PConclusion
    is_template: bool | None = None
    missing_elements: tuple[str, ...] = field(default_factory=tuple)
    reviewer: str | None = None


def template_component(confirmations: list[PConfirmation]) -> RatioComponent:
    """P = 经人工确认「模板化且缺可验证要素」的表述数 / 已确认的实质经营表述数。

    ⚠ 会计口径 §4.4：「所有进入指数的实质经营表述必须**完成逐条人工确认**；
    不能只抽查后把未审条目视为无问题。」

    所以只要还有 `pending` 的实质表述，P 就是**不完整**——
    哪怕已经确认的那些都判了 no_penalty。抽查过的部分看起来完美，
    没抽查的部分才是重点，而这一点从比例上完全看不出来。
    """
    substantive = [c for c in confirmations if c.is_substantive]
    non_substantive = [c for c in confirmations if not c.is_substantive]

    pending = [c for c in substantive if c.conclusion == "pending"]
    reviewed = [c for c in substantive if c.conclusion != "pending"]
    penalised = [c for c in reviewed if c.conclusion == "p_penalty"]
    needs_review = [c for c in reviewed if c.conclusion == "needs_review"]

    problems: list[str] = []
    if pending:
        problems.append(f"{len(pending)} 条实质表述尚未人工确认")
    if needs_review:
        problems.append(f"{len(needs_review)} 条待复核（口径或证据有冲突）")
    if not confirmations:
        problems.append("没有任何确认记录")

    return RatioComponent(
        name="template_penalty",
        label_cn="缺乏可验证性 P",
        numerator=len(penalised),
        denominator=len(reviewed),
        verified=not problems and bool(reviewed),
        note="；".join(problems)
        + (f"（另有 {len(non_substantive)} 条已判定为非实质表述，不进分母）"
           if non_substantive else ""),
    )


# ---------------------------------------------------------------- 汇总


@dataclass(frozen=True)
class AttestationSummary:
    """R 与 P 的完成度。给页面用的「还差多少」。"""

    risk_items_total: int
    risk_items_resolved: int
    substantive_claims: int
    substantive_confirmed: int

    @property
    def risk_complete(self) -> bool:
        return self.risk_items_resolved == self.risk_items_total

    @property
    def p_complete(self) -> bool:
        return self.substantive_claims == self.substantive_confirmed

    def describe(self) -> str:
        parts = []
        parts.append(
            f"R {self.risk_items_resolved}/{self.risk_items_total} 项已核验"
        )
        parts.append(
            f"P {self.substantive_confirmed}/{self.substantive_claims} 条实质表述已确认"
        )
        return "；".join(parts)


def summarize(
    risk_items: list[RiskItemResult], confirmations: list[PConfirmation]
) -> AttestationSummary:
    substantive = [c for c in confirmations if c.is_substantive]
    return AttestationSummary(
        risk_items_total=len(RISK_ITEMS),
        risk_items_resolved=sum(1 for i in risk_items if i.resolved),
        substantive_claims=len(substantive),
        substantive_confirmed=sum(1 for c in substantive if c.conclusion != "pending"),
    )
