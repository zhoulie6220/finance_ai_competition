"""诊断指数的测试。

验收案例逐字抄自会计口径 v1.1 §A.9：

    H=0、C=0、R=0、P=0、Q=0，且各项确有可计算证据  → I = 50
    H=1、C=1、R=1、P=0、Q=0                        → I = 95
    H=−1、C=−1、R=0、P=1、Q=1                      → 原值 −15，截断为 0
    6/10 条有效，H/C 均有观测且其它分项完整         → 覆盖闸门通过
    5/10 条有效                                    → 不通过
    4/5 条有效                                     → 不通过最少观测闸门
    全部都是本期主张，历史没有可验证观测            → 不输出完整指数
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.index import (
    INSUFFICIENT,
    IndexConfig,
    IndexInput,
    RatioComponent,
    compute_index,
)

D = Decimal
CFG = IndexConfig()


def comp(
    name: str,
    label: str,
    num: int | None,
    den: int | None,
    *,
    verified: bool = True,
    note: str = "",
) -> RatioComponent:
    return RatioComponent(
        name=name, label_cn=label, numerator=num, denominator=den,
        verified=verified, note=note,
    )


def perfect_rpq(
    r_num: int = 0, q_num: int = 0
) -> tuple[RatioComponent, RatioComponent, RatioComponent]:
    """R / P / Q 三项都可算且核验完成。

    R 的分母是 4（v1.1 固定的四项风险检查），Q 的是 3（固定三项质量检查）。
    分子分开给——共用一个的话 R 与 Q 会同时变，算出来的分数对不上公式。
    """
    return (
        comp("risk_shift", "风险披露充分度 R", r_num, 4),
        comp("template_penalty", "缺乏可验证性 P", 0, 20),
        comp("quality_conflict", "财务质量冲突 Q", q_num, 3),
    )


def data(
    *,
    h: tuple[Decimal, ...] = (D(0),),
    c: tuple[Decimal, ...] = (D(0),),
    n: int = 6,
    N: int = 10,
    rpq: tuple[RatioComponent, RatioComponent, RatioComponent] | None = None,
) -> IndexInput:
    risk, template, quality = rpq or perfect_rpq()
    return IndexInput(
        history_scores=h,
        current_scores=c,
        observation_count=n,
        denominator_count=N,
        risk=risk,
        template=template,
        quality=quality,
    )


# ---------------------------------------------------------------- 验收案例


def test_v11_all_zero_components_score_50() -> None:
    """★ H=C=R=P=Q=0 且各项确有可计算证据 → 50。

    这一条同时证明了 H、C 不再是 [0,1]：旧口径下中性是 0.5，
    这里会算出 70 分——**正好压在「一致性较高」的分界线上**。
    """
    result = compute_index(data(), CFG)
    assert result.ok
    assert result.score == D("50.000000")
    assert result.grade == "medium"


def test_v11_all_positive_scores_95() -> None:
    """H=1、C=1、R=1、P=0、Q=0 → 50+20+20+5 = 95。"""
    result = compute_index(
        data(h=(D(1),), c=(D(1),), rpq=perfect_rpq(r_num=4)), CFG
    )
    assert result.ok
    assert result.score == D("95.000000")
    assert result.grade == "high"


def test_v11_all_negative_clips_to_zero() -> None:
    """H=−1、C=−1、R=0、P=1、Q=1 → 50−20−20−10−15 = −15，截断为 0。"""
    result = compute_index(
        data(
            h=(D(-1),),
            c=(D(-1),),
            rpq=(
                comp("risk_shift", "风险披露充分度 R", 0, 4),
                comp("template_penalty", "缺乏可验证性 P", 20, 20),
                comp("quality_conflict", "财务质量冲突 Q", 3, 3),
            ),
        ),
        CFG,
    )
    assert result.ok
    assert result.score == D("0.000000")
    assert result.grade == "low"


def test_v11_coverage_gate() -> None:
    """6/10 通过；5/10 不通过。"""
    assert compute_index(data(n=6, N=10), CFG).ok is True

    failed = compute_index(data(n=5, N=10), CFG)
    assert failed.ok is False
    assert "覆盖率" in (failed.insufficient_reason or "")


def test_v11_min_observations_gate() -> None:
    """4/5 条有效 → 覆盖率够了（80%），但观测数不够。"""
    failed = compute_index(data(n=4, N=5), CFG)
    assert failed.ok is False
    assert "有效观测" in (failed.insufficient_reason or "")


def test_v11_current_only_claims_do_not_produce_a_full_index() -> None:
    """★ 全部都是本期主张、历史没有可验证观测 → 不输出完整指数。

    拿 0 去填 H 等于把「没评估」当成「无异议」——历史兑现度那一项
    会凭空变成中性，而它其实从没被评估过。
    """
    failed = compute_index(data(h=(), c=(D(1), D(0))), CFG)
    assert failed.ok is False
    assert "H（历史兑现度）没有观测" in (failed.insufficient_reason or "")


# ---------------------------------------------------------------- 闸门


@pytest.mark.parametrize("n,expected_ok", [(5, False), (6, True)])
def test_gate_boundary(n: int, expected_ok: bool) -> None:
    assert compute_index(data(n=n, N=10), CFG).ok is expected_ok


def test_gate_failure_produces_no_score() -> None:
    """★ 闸门不过时**绝不产出分数**。

    失败也给一个「带警告的数字」的话，页面一定会把它渲染成一个大号分数，
    人工复核的入口就形同虚设。
    """
    failed = compute_index(data(n=1, N=10), CFG)
    assert failed.score is None
    assert failed.grade == INSUFFICIENT
    assert failed.status == INSUFFICIENT
    assert failed.formula == "（未产出总分）"
    assert failed.insufficient_reason


def test_unverified_component_blocks_scoring() -> None:
    """★ 适用项未核完 → Q 不完整 → 不当作未触发 → 不出分。

    本批数据缺账龄明细，Q 的第二项必然走到这里。这是正确且诚实的结果，
    拿代理指标顶替会把「没查」变成「没问题」。
    """
    failed = compute_index(
        data(
            rpq=(
                comp("risk_shift", "风险披露充分度 R", 3, 4),
                comp("template_penalty", "缺乏可验证性 P", 0, 20),
                comp(
                    "quality_conflict", "财务质量冲突 Q", None, None,
                    verified=False, note="账龄明细未披露，适用项未核完",
                ),
            )
        ),
        CFG,
    )
    assert failed.ok is False
    assert "未核验完成" in (failed.insufficient_reason or "")
    assert failed.score is None


def test_zero_denominator_is_not_a_pass() -> None:
    """分母为 0 说明没有适用项，但**不能说这项通过了**——它只是没得算。"""
    failed = compute_index(
        data(
            rpq=(
                comp("risk_shift", "风险披露充分度 R", 0, 0, verified=False,
                     note="没有适用的风险检查项"),
                comp("template_penalty", "缺乏可验证性 P", 0, 20),
                comp("quality_conflict", "财务质量冲突 Q", 0, 3),
            )
        ),
        CFG,
    )
    assert failed.ok is False


def test_reason_names_every_failed_condition() -> None:
    """失败原因要把**所有**不满足的条件都列出来，不是只报第一条。

    只报第一条的话，补完一条重跑又发现还差一条，来回折腾。
    """
    failed = compute_index(data(h=(), c=(), n=1, N=10), CFG)
    reason = failed.insufficient_reason or ""
    assert "覆盖率" in reason
    assert "有效观测" in reason
    assert "H（历史兑现度）" in reason
    assert "C（当期一致性）" in reason


# ---------------------------------------------------------------- 旧口径防线


def test_half_values_blow_up() -> None:
    """★ s 值只能是 −1 / 0 / +1。

    喂进来 0.5 说明调用方还在用旧口径（中性编码成 0.5），那会让分数
    **虚高 10 分左右**，全中性的公司拿到 70 而不是 50，正好跨过
    「一致性较高」的分界线——而且不报任何错。
    """
    with pytest.raises(ValueError, match="旧口径"):
        compute_index(data(h=(D("0.5"),)), CFG)
    with pytest.raises(ValueError, match="旧口径"):
        compute_index(data(c=(D("0.5"),)), CFG)


def test_scores_are_not_clamped_to_zero_one() -> None:
    """★ H、C 的取值范围是 [−1, 1]，不能被截断到 [0, 1]。

    截断之后 −1 会变成 0，全相悖的公司拿到的分数与全中性的一样。
    """
    negative = compute_index(data(h=(D(-1),), c=(D(-1),)), CFG)
    neutral = compute_index(data(h=(D(0),), c=(D(0),)), CFG)
    assert negative.history == D("-1.000000")
    assert negative.score != neutral.score


def test_mean_of_mixed_scores() -> None:
    """等权平均：(1 + 0 + −1) / 3 = 0。"""
    result = compute_index(data(h=(D(1), D(0), D(-1)), c=(D(1), D(1))), CFG)
    assert result.history == D("0.000000")
    assert result.current == D("1.000000")


# ---------------------------------------------------------------- 分级与边界


def test_grade_boundaries() -> None:
    """分级按**未四舍五入**的值判。

    先舍入再比会出现 69.9996 显示成 70.00 却判 medium 的错位——
    页面显示 70.00、文案却说「支持与风险并存」。
    """
    # H=C=1 → 50+20+20 = 90（R/P/Q 全 0），落在 high
    assert compute_index(data(h=(D(1),), c=(D(1),)), CFG).grade == "high"
    # H=C=0 → 50，落在 medium
    assert compute_index(data(), CFG).grade == "medium"
    # H=C=−1 → 50−40 = 10，落在 low
    assert compute_index(data(h=(D(-1),), c=(D(-1),)), CFG).grade == "low"


def test_config_rejects_inverted_grade_lines() -> None:
    with pytest.raises(ValueError, match="中间那一档"):
        IndexConfig(grade_high_min=D("45"), grade_low_max=D("70"))


def test_config_rejects_absurd_coverage() -> None:
    with pytest.raises(ValueError):
        IndexConfig(min_coverage=D("1.5"))


def test_result_carries_the_formula_for_the_evidence_chain() -> None:
    """「点击结论回到计算过程」靠 formula。缺了它结论就不可追溯。"""
    result = compute_index(data(h=(D(1),), c=(D(0),)), CFG)
    assert "I = 50" in result.formula
    assert "H(" in result.formula and "Q(" in result.formula


def test_conclusion_boundary_is_always_present() -> None:
    """结论边界模板必须随结果一起给出，且**不由模型生成**。"""
    for result in (compute_index(data(), CFG), compute_index(data(n=1, N=10), CFG)):
        assert "不构成审计意见或证券买卖建议" in result.conclusion_boundary
        assert "不代表管理层诚信" in result.conclusion_boundary


def test_coverage_line_reads_correctly() -> None:
    result = compute_index(data(n=6, N=10), CFG)
    assert result.coverage_line() == "覆盖率 6/10 = 0.600000"
