"""会计签字文档（accounting_signoff_v1）各项决定的回归测试。

这些条目和 `test_schema_integrity.py` 里的缺陷是同一类：**做错了不会报错**。
比如把「资产注入年度」标记为不可比，如果 schema 的取值表里没有这个值，
你会得到一句 `CHECK constraint failed` —— 那还算好的；更常见的是发现标记不上
之后干脆放弃标记，于是那个被并购撑起来的年份就**静默留在了主中枢里**，
把周期中枢算高，最终抬高 DCF 的起点。

所以这里的断言一律打在「能不能真的写进去」上，而不是去比对 DDL 文本。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db.session import connect_memory, init_schema, load_seeds
from app.engine.normalization import NormalizationConfig
from app.schemas.enums import IncomparableReason, ParamTier


@pytest.fixture()
def con() -> sqlite3.Connection:
    """装**真实种子数据**的库。断言的对象是仓库里的实际内容，不是构造样例。"""
    c = connect_memory()
    init_schema(c)
    load_seeds(c)
    c.execute(
        "INSERT INTO project (project_id, name, company_name, stock_code, industry,"
        " fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
        ("p1", "测试", "某钢铁", "600019.SH", "steel", '["2024"]', "2026-09-01T00:00:00"),
    )
    c.execute(
        "INSERT INTO file (file_id, project_id, role, period, rel_path, sha256, bytes,"
        " uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
        ("f1", "p1", "annual_report", "2024", "samples/a.pdf", "abc", 100,
         "2026-09-01T00:00:00"),
    )
    yield c
    c.close()


def insert_fact(con: sqlite3.Connection, **over: object) -> None:
    vals: dict[str, object] = dict(
        fact_id="k1", project_id="p1", company_id="c1", is_primary=1,
        metric_key="revenue", value_millions="100", unit="百万元", period="2024",
        period_kind="current", scope="consolidated", source_file="2024年年度报告.pdf",
        source_file_id="f1", source_page=86, source_text="营业收入 100",
        confidence=0.96, status="validated", extractor="rule:v1",
        created_at="2026-09-01T00:00:00",
    )
    vals.update(over)
    cols = ",".join(vals)
    con.execute(
        f"INSERT INTO financial_fact ({cols}) VALUES ({','.join('?' * len(vals))})",
        list(vals.values()),
    )


# ------------------------------------------------------------------ §4 不可比原因


def test_every_declared_incomparable_reason_is_writable(con: sqlite3.Connection) -> None:
    """§4 的每一个不可比原因都必须真的能写进 financial_fact。

    这条曾经是坏的：Pydantic 放行 `asset_injection`（资产注入），
    而 financial_fact 的 CHECK 里没有它 —— 签字文档 §4 明确要求标记的
    「资产注入年度」根本标不上。检测的方式只能是逐个真写一遍：
    比对 DDL 文本会被 CHECK 的换行和缩进绊倒，而且改了一处漏改另一处时
    照样看不出来。
    """
    for i, reason in enumerate(IncomparableReason):
        con.execute("SAVEPOINT sp")
        insert_fact(
            con,
            fact_id=f"t{i}",
            # 自然键含 period，不换一个的话第二条就撞 UNIQUE 了 ——
            # 那会掩盖真正要测的 CHECK 行为
            period=f"20{10 + i:02d}",
            comparable=0,
            incomparable_reason=reason.value,
        )
        con.execute("RELEASE sp")


def test_normalization_year_accepts_the_same_reasons(con: sqlite3.Connection) -> None:
    """normalization_year 与 financial_fact 必须认同同一套取值。

    两张表存的其实是同一件事：一个是「这笔事实不可比」，一个是「这一年的数据不可比」。
    取值表分叉的话，同一家公司的同一次并购，在事实层标得上、在年度层标不上，
    结果就是那一年照样进了主中枢。
    """
    con.execute(
        "INSERT INTO normalization_run (normalization_id, project_id, window_mode,"
        " preferred_years, fallback_years, min_comparable_years, ebit_formula,"
        " status, insufficient_reason, method_version, rule_config_version, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        # 用 incomplete_cycle 而不是 normalized：status='normalized' 会被 CHECK 要求
        # 「coverage_passed=1 且 ebit_margin_mid 非空」，那是正常化失败时强制不出分
        # 的那条硬规则。本测试只关心不可比原因的取值表，不打算构造一次成功的正常化。
        # 拒绝出分时必须写明原因，这也是 CHECK 强制的 —— 失败状态不能是静默的。
        ("n1", "p1", "primary_8y", 8, 10, 7, "利润总额 + 利息费用 - 利息收入",
         "incomplete_cycle", "测试用：仅验证不可比原因的取值表", "v1", 1,
         "2026-09-01T00:00:00"),
    )
    for i, reason in enumerate(IncomparableReason):
        con.execute("SAVEPOINT sp")
        con.execute(
            "INSERT INTO normalization_year (id, normalization_id, year,"
            " comparable, incomparable_reason, included_in_median)"
            " VALUES (?,?,?,?,?,?)",
            (f"y{i}", "n1", f"20{10 + i:02d}", 0, reason.value, 0),
        )
        con.execute("RELEASE sp")


def test_unknown_incomparable_reason_is_rejected(con: sqlite3.Connection) -> None:
    """反面：不在取值表里的原因必须被拒。

    否则「不可比原因」会变成一列自由文本，写进「随便什么」也照样通过，
    而这个字段的全部价值就在于它是**可枚举、可统计、可复核**的。
    """
    with pytest.raises(sqlite3.IntegrityError):
        insert_fact(con, comparable=0, incomparable_reason="因为我觉得不行")


# ------------------------------------------------------------------ §6 例句溯源


def test_annual_report_example_without_provenance_is_rejected(
    con: sqlite3.Connection,
) -> None:
    """§6：标为年报原文就必须有 PDF 文件名与页码。

    与 financial_fact 的硬规则一同源 —— 无来源不得进已验证。
    例句是别名维护的锚点，也是评委核对字典的入口，它同样是证据。
    """
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "UPDATE metric_definition SET example_source='annual_report',"
            " example_sentence='营业收入为 1,234 元。' WHERE metric_key='revenue'"
        )


def test_annual_report_example_with_provenance_is_accepted(
    con: sqlite3.Connection,
) -> None:
    """齐备时必须放行 —— 否则会计同学补完例句反而写不进去。"""
    con.execute(
        "UPDATE metric_definition SET example_source='annual_report',"
        " example_sentence='营业收入为 1,234 元。',"
        " example_file='600019_2024_年年度报告.pdf', example_page=86"
        " WHERE metric_key='revenue'"
    )
    row = con.execute(
        "SELECT example_file, example_page FROM metric_definition WHERE metric_key='revenue'"
    ).fetchone()
    assert row["example_page"] == 86


def test_example_page_must_be_positive(con: sqlite3.Connection) -> None:
    """页码 0 或负数说明是「随手填了个值让它通过校验」。

    真实的年报页码从 1 开始，`source_page > 0` 是同一套写法。
    """
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "UPDATE metric_definition SET example_file='a.pdf', example_page=0"
            " WHERE metric_key='revenue'"
        )


# ------------------------------------------------------------------ A-1 旧键名迁移


def test_legacy_keys_cannot_be_written_as_facts(con: sqlite3.Connection) -> None:
    """A-1：旧键名不再写入新数据。

    队友按早先的设计文档写出 'net_profit' 时，必须**写不进去**。
    """
    for legacy in ("net_profit", "total_profit", "fixed_assets", "trade_receivables"):
        with pytest.raises(sqlite3.IntegrityError):
            insert_fact(con, metric_key=legacy)


def test_every_migration_target_exists(con: sqlite3.Connection) -> None:
    """映射表指向的字段必须真实存在，否则迁移提示会说「已改名为 <不存在的键>」。"""
    rows = con.execute(
        "SELECT m.legacy_key, m.metric_key FROM metric_key_migration m"
        " LEFT JOIN metric_definition d ON d.metric_key = m.metric_key"
        " WHERE d.metric_key IS NULL"
    ).fetchall()
    assert not rows, f"迁移表指向了不存在的字段：{[tuple(r) for r in rows]}"


def test_migration_covers_every_key_named_in_the_signoff(con: sqlite3.Connection) -> None:
    """签字文档 A-1 列的 13 项改名 + A-3 的 trade_receivables，一项都不能漏。

    漏一项的后果不是报错，而是那位队友的代码静默查不到数据 ——
    看起来和「公司没披露这一项」一模一样。
    """
    expected = {
        "total_profit": "profit_before_tax",
        "net_profit": "net_income",
        "net_profit_attr_parent": "net_income_parent",
        "deducted_net_profit": "non_gaap_net_income",
        "financial_expense": "finance_expense",
        "equity_attr_parent": "equity_parent",
        "contract_liability": "contract_liabilities",
        "fixed_assets": "ppe",
        "asset_impairment": "impairment_loss",
        "nonrecurring_pl": "non_recurring_gain_loss",
        "gov_subsidy": "government_grant",
        "asset_disposal_gain": "asset_disposal_gain_loss",
        "ton_steel_gross_margin": "steel_gross_profit_per_ton",
        "trade_receivables": "notes_and_ar",
    }
    actual = {
        r["legacy_key"]: r["metric_key"]
        for r in con.execute("SELECT legacy_key, metric_key FROM metric_key_migration")
    }
    assert actual == expected


def test_migration_key_is_not_itself_a_live_metric(con: sqlite3.Connection) -> None:
    """旧键名不能同时又是活字段 —— 那等于改名没改干净。"""
    clash = con.execute(
        "SELECT m.legacy_key FROM metric_key_migration m"
        " JOIN metric_definition d ON d.metric_key = m.legacy_key"
    ).fetchall()
    assert not clash, f"这些旧键名仍然存在：{[r[0] for r in clash]}"


def test_no_live_column_is_named_after_a_legacy_key(con: sqlite3.Connection) -> None:
    """A-1 的后半句：「不再写入新数据」——包括**表结构本身**。

    这条是被真实情况逼出来的：metric_definition 改完名之后，
    normalization_year 里还留着 total_profit 和 financial_expense 两列。
    判断「利润总额」在字典里叫 profit_before_tax、在这张表里叫 total_profit，
    靠的是读者自己记住它们是一回事 —— 这种「同一个东西两个名字」正是
    改名没改干净的样子，而且永远不会报错。

    扫 PRAGMA table_info 而不是读 DDL 文本：列名是结构化信息，
    文本匹配会被注释和格式绊倒。
    """
    tables = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts_%'"
        )
    ]
    legacy = [
        r[0] for r in con.execute("SELECT legacy_key FROM metric_key_migration")
    ]
    offenders: list[str] = []
    for table in tables:
        cols = {c["name"] for c in con.execute(f"PRAGMA table_info({table})")}
        for name in cols & set(legacy):
            offenders.append(f"{table}.{name}")
    assert not offenders, (
        f"这些表结构里还留着旧键名：{offenders}。"
        "旧键名应当只出现在 metric_key_migration 里。"
    )


def test_migration_has_an_accounting_decision_behind_it(con: sqlite3.Connection) -> None:
    """每次改名都要记明依据（哪份签字文档的哪一条）。

    评委问「为什么把 fixed_assets 改成 ppe」时，答案必须能从库里查出来。
    """
    rows = con.execute(
        "SELECT legacy_key, decided_by FROM metric_key_migration"
        " WHERE decided_by IS NULL OR length(trim(decided_by)) = 0"
    ).fetchall()
    assert not rows, f"这些改名没有记录依据：{[r[0] for r in rows]}"


# ------------------------------------------------------------------ A-2 来源映射


def test_mapped_from_must_point_to_a_real_metric(con: sqlite3.Connection) -> None:
    """A-2：mapped_from 指向实际抽到的字段，必须是真字段。"""
    with pytest.raises(sqlite3.IntegrityError):
        insert_fact(con, mapped_from="并不存在的字段")


def test_mapped_from_records_the_total_revenue_fallback(con: sqlite3.Connection) -> None:
    """只披露营业总收入时，值进 revenue，同时记下它其实来自 total_revenue。

    记下来是为了聚合前能识别出来：A-2 明确要求 revenue 与 total_revenue
    **不得相加**，而没有这一列的话，一组 (revenue, 2024) 的数据
    根本看不出它是营业收入还是营业总收入。
    """
    insert_fact(con, fact_id="f_rev", mapped_from="total_revenue")
    row = con.execute(
        "SELECT metric_key, mapped_from FROM financial_fact WHERE fact_id='f_rev'"
    ).fetchone()
    assert row["metric_key"] == "revenue"
    assert row["mapped_from"] == "total_revenue"


# ------------------------------------------------------------------ A-6 周期阈值


def test_phase_spread_in_config_matches_the_engine_default(con: sqlite3.Connection) -> None:
    """A-6 的「高低盈利阶段最低落差 2 个百分点」必须与引擎默认值一致。

    `rule_config` 是页面上「查看 / 修改 / 恢复默认」那一份，`NormalizationConfig`
    是计算时实际用的那一份。两者不一致的话，页面上写着 2%，算了半天用的却是别的数，
    而且**不会有任何提示**。
    """
    row = con.execute(
        "SELECT value FROM rule_config WHERE key='normalization.phase_min_spread'"
    ).fetchone()
    assert row is not None, "rule_config 里没有 normalization.phase_min_spread"
    assert row["value"] == str(NormalizationConfig().min_cycle_amplitude)


def test_quantile_phase_params_are_gone(con: sqlite3.Connection) -> None:
    """分位数版的阶段判定参数必须已经不在了。

    留着它的危险不在于它没用，而在于**它看起来有用**：下一个人看到
    `high_phase_quantile=0.75`，会以为阶段判定是分位数的，于是照着它去实现，
    把一个恒为真的校验又搬回来。签字文档 A-6 定的是绝对落差，不是分位数。
    """
    rows = con.execute(
        "SELECT key FROM rule_config WHERE key LIKE '%phase_quantile%'"
    ).fetchall()
    assert not rows, f"分位数阶段参数应当已删除：{[r[0] for r in rows]}"


# ------------------------------------------------------------------ A-8 参数分级


def test_every_rule_config_tier_is_a_declared_value(con: sqlite3.Connection) -> None:
    """A-8：tier 只允许 hard / soft / model。

    分级决定改一个参数需要谁点头。取值不受约束的话，多打一个字母
    就会让一条硬规则**降级成不需要签字**，而且不会报错。
    """
    allowed = {t.value for t in ParamTier}
    bad = con.execute(
        "SELECT key, tier FROM rule_config WHERE tier NOT IN"
        f" ({','.join('?' * len(allowed))})",
        sorted(allowed),
    ).fetchall()
    assert not bad, f"这些参数的 tier 非法：{[tuple(r) for r in bad]}"


def test_hard_rules_carry_the_full_signoff_set(con: sqlite3.Connection) -> None:
    """A-8 点名的六类参数必须都在，并且都是 hard。

    用「按前缀数一数」而不是逐个列 key：新增一个周期阈值不该让测试变红，
    但整类参数消失必须变红。
    """
    tiers = {
        r["key"]: r["tier"]
        for r in con.execute("SELECT key, tier FROM rule_config")
    }
    assert tiers, "rule_config 是空的"
    # 这些前缀下不允许出现 soft/model —— 它们全是会计口径
    hard_prefixes = (
        "normalization.", "nonrecurring.", "valuation.",
        "index.weight", "index.penalty", "index.min_",
    )
    soft = sorted(k for k in tiers if k.startswith(hard_prefixes) and tiers[k] != "hard")
    assert not soft, f"这些参数属于会计口径，不能是非 hard：{soft}"
