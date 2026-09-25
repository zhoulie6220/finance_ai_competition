"""叙事—事实一致性引擎。

这个文件里的每一条断言都对应一个**真的发生过、且不报错**的缺陷：
过滤词写宽了、阈值写没了、年份挪错了。它们共同的表现是
「系统给出一个看起来很确定的判定，而它是错的」——比崩溃糟得多。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.narrative import (
    CONFLICTED,
    INCOMPARABLE,
    MISSING,
    SUPPORTED,
    Claim,
    MetricRef,
    Utterance,
    find_claims,
    summarize,
    verify,
    verify_all,
)

#: 一个能命中「降本增效」主题（验毛利率）的主张。
_COST_METRIC = MetricRef("gross_margin", "毛利率", "%")


def _claim(period: str = "2024", *, forward: bool = False, verify_period: str | None = None) -> Claim:
    return Claim(
        theme_key="cost",
        theme_label="降本增效",
        text="持续深化降本增效。",
        page_no=12,
        matched="降本增效",
        source_file="某公司：2024年年度报告.pdf",
        source_period=period,
        verify_period=verify_period or period,
        forward=forward,
        metric=_COST_METRIC,
    )


# ------------------------------------------------------------------ 抽取


def test_forward_section_verifies_the_next_year() -> None:
    """前瞻段说的是下一年，验证对象要往后挪一年。

    不挪的话，「2024 年报说 2025 年要降本」会拿 2024 年的数字去判，
    方向多半对不上，于是**所有尚未到期的计划都被判成冲突**——
    而那正是这个系统最该算对的一类主张（历史兑现度）。
    """
    claims = find_claims(
        [Utterance(text="2025年将深入推进降本增效。", page_no=30, period="2024", forward=True)]
    )
    assert len(claims) == 1
    assert claims[0].verify_period == "2025", "前瞻段没有往后挪一年"


def test_future_word_is_not_treated_as_negation() -> None:
    """「未来」不能被当成否定词。

    `_NEGATION` 里若写单字「未」，它会命中「**未**来」——而前瞻段里
    「未来」遍地都是，结果是整段展望被静默丢光，
    表现为「这家公司从不做前瞻性表述」。
    """
    claims = find_claims(
        [
            Utterance(
                text="未来公司将持续推进降本增效工作。",
                page_no=30, period="2024", forward=True,
            )
        ]
    )
    assert len(claims) == 1, "「未来」被误当成否定词，前瞻句被丢掉了"


def test_cost_decline_is_not_negation() -> None:
    """「成本下降」不能被当成否定词——它正是降本增效的正面措辞。

    把「下降」写进全局否定词，会把要验的主张本身过滤掉，
    表现为「这家公司从来不提降本」，而它其实年年都提。
    """
    claims = find_claims(
        [Utterance(text="工序成本下降明显，降本增效成效显著。", page_no=12, period="2024")]
    )
    assert len(claims) == 1


def test_risk_statement_is_not_a_claim() -> None:
    """风险陈述不是向好主张。"""
    claims = find_claims(
        [Utterance(text="行业需求低迷，订单饱满度不足。", page_no=9, period="2024")]
    )
    assert claims == []


def test_exclude_words_block_project_names() -> None:
    """带排除词的句子不算主张。

    宝钢 2024 的「结构优化」6 句里 5 句是「无取向硅钢产品结构优化**工程**」——
    说的是项目名。当主张收进来，界面上会显示成「管理层说产品结构要升级」，
    评审一看就知道系统没读懂。
    """
    claims = find_claims(
        [Utterance(text="资金主要用于无取向硅钢产品结构优化工程。", page_no=20, period="2024")]
    )
    assert claims == []


def test_same_page_hits_are_one_claim_per_theme() -> None:
    """同一页同一主题只算一条主张。

    一句话里命中两次很常见。按命中次数计数，会让话多的公司显得主张更多，
    而那只是它话多。
    """
    claims = find_claims(
        [Utterance(text="降本增效持续推进，成本削减目标落地。", page_no=12, period="2024")]
    )
    assert len(claims) == 1


# ------------------------------------------------------------------ 验证


def _series(**kw: str) -> dict[str, Decimal | None]:
    return {k.replace("_", ""): Decimal(v) for k, v in kw.items()}


def test_direction_up_supports_and_down_conflicts() -> None:
    up = verify(_claim("2024"), {"2023": Decimal("10"), "2024": Decimal("12")})
    assert up.state == SUPPORTED

    down = verify(_claim("2024"), {"2023": Decimal("14.99"), "2024": Decimal("10.88")})
    assert down.state == CONFLICTED


def test_tiny_move_is_not_a_conflict() -> None:
    """微小变动不算冲突。

    ⚠ 这是实测翻过的车：宝钢 2019 年毛利率从 10.8791% 走到 10.8350%，
    动了 0.04 个百分点，被判成「管理层说降本增效，事实相悖」。
    那种量级在会计上什么都不说明，而它和真正的背离（14.99% → 10.88%）
    在四态表里长得一模一样——评审一眼就会看出系统分不清噪声与信号。
    """
    ob = verify(_claim("2019"), {"2018": Decimal("10.8791"), "2019": Decimal("10.8350")})
    assert ob.state == INCOMPARABLE, "0.04 个百分点的变动被当成了冲突"
    assert "阈值" in ob.reason


def test_threshold_is_configurable() -> None:
    """阈值是参数，不是写死的常数——它是会计口径，得能调。"""
    series = {"2018": Decimal("10.8791"), "2019": Decimal("10.8350")}
    ob = verify(_claim("2019"), series, min_rel_change=Decimal("0.0001"))
    assert ob.state == CONFLICTED, "阈值调小后，同样的数据应当能判出方向"


def test_missing_year_is_reported_as_missing_not_incomparable() -> None:
    """「这一年没披露」与「算不了」是两件事，状态不能压成一个。

    用户对这两种情况要做的事完全不同：前者去翻年报，后者去查口径。
    """
    ob = verify(_claim("2024"), {"2023": Decimal("10"), "2024": None})
    assert ob.state == MISSING
    assert "2024" in ob.reason


def test_non_adjacent_years_are_refused() -> None:
    """隔年的数据不能拿来验——算出来的是两年的累计变化，却看起来和同比一样。

    ⚠ 必须显式传 `prev_period` 才测得到这条路径：`verify()` 默认取
    `验证年 − 1`，而 `{"2022":…, "2024":…}` 这种序列在默认路径下
    是「2023 年没数据」，判成 `missing` 而不是 `incomparable`。
    两者都要报得出来，但它们是不同的故障。
    """
    ob = verify(
        _claim("2024"), {"2022": Decimal("10"), "2024": Decimal("12")},
        prev_period="2022",
    )
    assert ob.state == INCOMPARABLE
    assert "不连续" in ob.reason


def test_flat_is_neither_supported_nor_conflicted() -> None:
    """持平判成冲突会凭空多出一处「叙事与事实相悖」。

    而那张四态表正是要拿去给评审看的，不能为了凑数把它塞进任何一边。
    """
    ob = verify(_claim("2024"), {"2023": Decimal("10"), "2024": Decimal("10")})
    assert ob.state not in (SUPPORTED, CONFLICTED)


def test_metric_absent_entirely_is_missing() -> None:
    ob = verify_all([_claim("2024")], {})
    assert ob[0].state == MISSING


def test_every_observation_carries_formula_and_inputs() -> None:
    """每个判定都要能被复算——这是「点结论回到计算过程」的路径。

    没有 formula 与 inputs 的判定，和一个凭空的判断在界面上长得一样。
    """
    for series in (
        {"2023": Decimal("10"), "2024": Decimal("12")},
        {"2023": Decimal("14.99"), "2024": Decimal("10.88")},
    ):
        ob = verify(_claim("2024"), series)
        assert ob.formula, "判定没有带公式"
        # 键里带「年」字是刻意的（与 engine.ratios.yoy_growth 一致）：
        # 证据面板上「2024 年」比「2024」更像一句人话，也和公式里的措辞对得上。
        assert "2023 年" in ob.inputs and "2024 年" in ob.inputs, "入参的键里没有年份"


# ------------------------------------------------------------------ 汇总


def test_summary_never_produces_a_score() -> None:
    """汇总只计数与陈述，**不出分**。

    指数公式（I = 50 + 20H + 20C + 5R − 10P − 15Q）里的 R/P/Q 三项
    还没接入（Q 依赖 engine/checks.py）。缺 30 分权重时算出来的分，
    与完整版长得一模一样，而它会被直接拿去用。
    """
    obs = verify_all(
        [_claim("2024")],
        {"gross_margin": {"2023": Decimal("10"), "2024": Decimal("12")}},
    )
    v = summarize(obs)
    assert not hasattr(v, "score"), "汇总里不该出现分值"
    assert "诊断指数" in v.text, "结论里要写明为什么不出分"


def test_summary_headline_reflects_conflicts() -> None:
    ok = summarize(verify_all([_claim("2024")],
                              {"gross_margin": {"2023": Decimal("10"), "2024": Decimal("12")}}))
    assert "一致" in ok.headline

    bad = summarize(verify_all([_claim("2024")],
                               {"gross_margin": {"2023": Decimal("14"), "2024": Decimal("9")}}))
    assert "相悖" in bad.headline


def test_no_claims_says_so_without_implying_the_company_is_fine() -> None:
    """没有主张 ≠ 公司没问题。

    措辞稍不注意就会写成「未发现异常」，而那句话会被当成一个结论——
    系统根本没读到东西和系统读完了没发现问题，是完全不同的两件事。
    """
    v = summarize([])
    assert "不代表" in v.text or "不意味" in v.text


@pytest.mark.parametrize("theme_key", ["demand", "capacity", "cost", "mix", "cash"])
def test_every_theme_has_a_verifiable_metric(theme_key: str) -> None:
    """主题表里每条都得指向一个指标，否则那条主张永远验不了。"""
    from app.engine.narrative import THEMES

    t = THEMES[theme_key]
    assert t.metric.key, f"{theme_key} 没有配指标"
    assert t.basis_cn, f"{theme_key} 没写为什么用这个指标验"
