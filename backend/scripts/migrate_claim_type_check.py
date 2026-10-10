"""把 `claim.claim_type` 的 CHECK 约束加上 `'management_budget'`，就地改现有库。

用法：
    python scripts/migrate_claim_type_check.py            # 只打印计划（dry-run）
    python scripts/migrate_claim_type_check.py --apply    # 真的改表
    python scripts/migrate_claim_type_check.py --seed     # 只插那条 rule_config

## 为什么不能跑 `init_db.py --force`

它是「重建」不是「补齐」：`financial_fact`（上千条事实）与 `mdna_section`
（几百段正文）会一起清空，而那两样**只此一份**，要重新解析才回得来。
所以 schema 的改动必须就地落到现有库上。

## ⚠ 为什么这个脚本自己开连接，不走 `app.db.session.connect()`

`connect()` 在每条连接上强制 `PRAGMA foreign_keys = ON` 并自检。
而 `claim` 是**父表**——`claim_indicator` / `claim_match` / `p_confirmation` /
`p_prediction` 都 `REFERENCES claim(claim_id) ON DELETE CASCADE`。
在开着外键的连接上 `DROP TABLE claim` 会触发隐式 DELETE，
**把会计那两百多行人工确认一起级联删掉**，而且看起来一切正常。

所以这里必须自己控 PRAGMA：先 `OFF`，改完表结构再 `ON`，
最后用 `PRAGMA foreign_key_check` 证明没有留下悬空引用。
（`app.db.session.connect()` 拿到的连接做不了这件事——它的自检就在入口。）

## 改 CHECK 为什么要重建整张表

SQLite 不支持 `ALTER TABLE ... ALTER COLUMN`，改 CHECK 的标准做法是
官方文档里那 12 步：建新表 → 拷数据 → 删旧表 → 改名 → 重建索引 → 查外键。
下面按这个顺序走，并且在**每一项**上都对账（行数、列集合），
因为「拷漏了几行」和「拷全了」在页面上看起来一模一样。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402

_console.setup()

BACKEND = Path(__file__).resolve().parent.parent
SCHEMA = BACKEND / "app" / "db" / "schema.sql"
DB = BACKEND / "var" / "finance.db"
BACKUP = BACKEND / "var" / "finance.db.bak-before-claim-type-check"

#: 重建前后都要对账的表。前面四张是 `claim` 的子表（**最怕丢的就是它们**），
#: 后面几张是不可再生的数据，用来证明确实没动到别处。
WATCH = (
    "claim",
    "claim_indicator",
    "claim_match",
    "p_confirmation",
    "p_prediction",
    "financial_fact",
    "mdna_section",
    "document_page",
    "risk_disclosure_check",
    "q2_aging",
)

NEW_TYPE = "management_budget"

#: 那条按项目声明的开关。与 `app/data/seed/0002_rule_config.sql` 里新增的那一行
#: 逐字一致——种子文件管新建库，这里管现有库，两处必须同时改。
SEED_ROW = (
    f"narrative.budget_attainment.p-000959", "1", "bool", "",
    "受限历史计划兑现度（首钢股份）",
    "为 1 时，把「N、20XX年经营计划 → 财务指标预算安排」里的**公司总营业收入**预算，"
    "与目标年度的实际营业收入对比，作为受限的历史计划兑现观测**计入 H**。"
    "判定为 supported（实际 ≥ 预算）/ contradicted（实际 < 预算），不套噪声带。"
    "六个纳入条件：①预算在经营计划标题下披露；②目标年度明确、指标为公司营业收入；"
    "③实际数取目标年度年报合并利润表；④预算与实际属同一合并范围"
    "（**编制该预算的报告年必须晚于合并范围断点，否则排除**）；⑤目标年度已结束；"
    "⑥比公司总额，子公司分项不相加。它表示目标达成度，"
    "**不等同于预测准确率或管理层可信度**。",
    "", "1", "0", "1",
)


def _new_ddl() -> str:
    """从 schema.sql 里抠出 `claim` 的建表语句，改名为 `claim_new`。"""
    sql = SCHEMA.read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE claim \(.*?\n\);", sql, re.S)
    if m is None:
        raise SystemExit("✗ schema.sql 里找不到 claim 的建表语句——它被改过？")
    ddl = m.group(0)
    if NEW_TYPE not in ddl:
        raise SystemExit(
            f"✗ schema.sql 的 claim_type CHECK 里没有 '{NEW_TYPE}'。"
            f"**先改 schema.sql 再来迁移**，否则新表建出来和老表一样。"
        )
    return ddl.replace("CREATE TABLE claim (", "CREATE TABLE claim_new (", 1)


def _counts(con: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in WATCH:
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = -1
    return out


def _live_columns(con: sqlite3.Connection) -> list[str]:
    return [r[1] for r in con.execute("PRAGMA table_info(claim)")]


def dependent_views(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """引用了 `claim` 的视图（名字 + 建视图语句）。

    ⚠ **必须先把它们摘掉。** 删表本身不会报错，但 `ALTER TABLE ... RENAME`
    会**重新解析整个 schema**——包括视图。视图指着一张刚被删掉的表时，
    RENAME 直接失败：

        sqlite3.OperationalError: error in view v_match_evidence:
                                 no such table: main.claim

    实测撞到过：整个迁移在 RENAME 那一步炸掉并回滚（没有损坏，但白白跑一遍）。
    """
    out: list[tuple[str, str]] = []
    for name, sql in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'view'"
    ):
        if re.search(r"\bclaim\b", sql or ""):
            out.append((name, sql))
    return out


def _new_columns(ddl: str) -> list[str]:
    """新表的列名 + 顺序。用内存库把 DDL 执行一遍拿 PRAGMA——比正则稳。"""
    mem = sqlite3.connect(":memory:")
    try:
        mem.execute(ddl)
        return [r[1] for r in mem.execute("PRAGMA table_info(claim_new)")]
    finally:
        mem.close()


def plan(con: sqlite3.Connection) -> tuple[dict[str, int], list[str], list[str]]:
    before = _counts(con)
    live = _live_columns(con)
    new = _new_columns(_new_ddl())
    return before, live, new


def apply_to(con: sqlite3.Connection) -> int:
    """真的改。返回 0 成功 / 非 0 失败。调用方的连接必须**外键已关**。"""
    before, live, new = plan(con)
    if live != new:
        print("✗ 新表的列与现有表不一致，**不动**：")
        print(f"    只有老表有：{sorted(set(live) - set(new))}")
        print(f"    只有新表有：{sorted(set(new) - set(live))}")
        print("  列对不上就不能用 `INSERT INTO new SELECT * FROM old`——会静默错位。")
        return 1

    views = dependent_views(con)
    con.execute("BEGIN")
    try:
        # 先摘视图，见 `dependent_views` 的说明。**按原样存下来再装回去**——
        # 这次迁移的职责只有「换 claim 的一张 CHECK」，不顺手改别的东西。
        for name, _ in views:
            con.execute(f"DROP VIEW {name}")
        con.execute(_new_ddl())
        con.execute("INSERT INTO claim_new SELECT * FROM claim")
        n_new = con.execute("SELECT COUNT(*) FROM claim_new").fetchone()[0]
        if n_new != before["claim"]:
            raise RuntimeError(
                f"拷进新表的行数 {n_new} != 旧表 {before['claim']}——"
                f"不许继续（删了旧表就回不来了）"
            )
        con.execute("DROP TABLE claim")
        con.execute("ALTER TABLE claim_new RENAME TO claim")
        con.execute(
            "CREATE INDEX ix_claim_project ON claim(project_id, claim_type)"
        )
        for name, sql in views:
            con.execute(sql)
        con.commit()
    except Exception:
        con.rollback()
        print("✗ 迁移失败，已整体回滚（旧表原封不动）")
        raise
    print(f"    （摘掉又装回了 {len(views)} 个引用 claim 的视图："
          f"{', '.join(n for n, _ in views) or '无'}）")
    return 0


#: 一条只填必填列的最小 claim，用来试 CHECK 到底生不生效。
_PROBE = (
    "INSERT INTO claim (claim_id, project_id, section_id, claim_text, claim_type,"
    " verifiable, confidence, source_file_id, source_page, source_text, extractor,"
    " prompt_version, status, created_at)"
    " VALUES (?, 'p', 's', 't', ?, 1, 0.5, 'f', 1, 't', 'e', 'v', 'validated', 'n')"
)


def verify(con: sqlite3.Connection, before: dict[str, int]) -> int:
    """后置自检。`con` 必须是**普通连接**（`app.db.session.connect()`）。"""
    ok = True
    if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        print("✗ 外键没开")
        ok = False
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        print(f"✗ 外键检查有 {len(bad)} 条悬空引用：{bad[:5]}")
        ok = False
    after = _counts(con)
    for t, n in before.items():
        if after.get(t) != n:
            # `claim` 自己也不该变——重建是「换结构」，不是「改数据」
            print(f"✗ {t}: {n} → {after.get(t)}")
            ok = False

    # 视图必须**一个不少地装回来**。少了的话，依赖它的接口从此 500，
    # 而「少一个视图」在迁移日志里看不出任何异常。
    want = set(re.findall(
        r"CREATE VIEW (\w+)", SCHEMA.read_text(encoding="utf-8")
    ))
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    if want - have:
        print(f"✗ 视图丢了：{sorted(want - have)}")
        ok = False
    if have != want:
        print(f"  （库里的视图与 schema.sql 不同：多 {sorted(have - want)}）")

    # 新值真的能插进去、自造的非法值仍被拒——**正反两面都要试**。
    # 只试正面的话，「CHECK 被整条删掉」也会通过，而那正是这次要防的。
    #
    # ⚠ 内存库是空库，探针引用的 project / mdna_section / file 都不存在，
    # 所以**必须先把外键关掉**——不关的话失败原因是 FOREIGN KEY 而不是 CHECK，
    # 看起来「新值插不进去」，实际是探针自己写错了（第一次就踩了这个）。
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(SCHEMA.read_text(encoding="utf-8"))
        mem.execute("PRAGMA foreign_keys = OFF")
        try:
            mem.execute(_PROBE, ("probe-ok", NEW_TYPE))
        except sqlite3.IntegrityError as exc:
            print(f"✗ 新值 '{NEW_TYPE}' 仍然插不进去：{exc}")
            ok = False
        try:
            mem.execute(_PROBE, ("probe-bad", "自造的假类型"))
            print("✗ 自造的假类型居然插进去了——CHECK 形同虚设")
            ok = False
        except sqlite3.IntegrityError:
            pass
    finally:
        mem.close()
    return 0 if ok else 1


def insert_seed_row(con: sqlite3.Connection) -> int:
    """把那条 `narrative.budget_attainment.p-000959` 插进现有库。

    ⚠ `init_db.py` **没有**「只补种子」这一档（不带 --force 直接退出，
    带 --force 整库重建），所以现有库的种子增量只能这样落。
    两边（种子文件 / 这里）必须同时改，否则新建的库和这个库分叉。
    """
    exists = con.execute(
        "SELECT 1 FROM rule_config WHERE key = ? AND industry = ''", (SEED_ROW[0],)
    ).fetchone()
    if exists:
        print(f"  {SEED_ROW[0]} 已存在，跳过")
        return 0
    con.execute(
        "INSERT INTO rule_config (key, value, value_type, industry, label_cn,"
        " description, unit, default_value, min_value, max_value)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (SEED_ROW[0], SEED_ROW[1], SEED_ROW[2], SEED_ROW[3], SEED_ROW[4],
         SEED_ROW[5], SEED_ROW[6], SEED_ROW[7], SEED_ROW[8], SEED_ROW[9]),
    )
    con.commit()
    print(f"  ✓ 已插入 {SEED_ROW[0]} = {SEED_ROW[1]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="claim.claim_type CHECK 就地迁移")
    ap.add_argument("--apply", action="store_true", help="真的改表（默认 dry-run）")
    ap.add_argument("--seed", action="store_true", help="插入新的 rule_config 行")
    args = ap.parse_args()
    if not (args.apply or args.seed):
        print("（dry-run：什么都没改。加 --apply 改表，加 --seed 插规则行）\n")

    if not DB.exists():
        print(f"✗ 找不到 {DB}")
        return 1

    con = sqlite3.connect(DB)
    try:
        before, live, new = plan(con)
        print(f"库：{DB}")
        print(f"现有列 {len(live)} 个；新表列 {len(new)} 个"
              f"；列集合{'一致 ✓' if live == new else '**不一致 ✗**'}")
        print("对账表（迁移前后必须逐一相等）：")
        for t, n in before.items():
            print(f"  {t:24s} {n}")
        if args.seed:
            print()
            if insert_seed_row(con) != 0:
                return 1
        if args.apply:
            if not BACKUP.exists():
                print(f"\n✗ 没有备份 {BACKUP}。**先备份再改表**。")
                return 1
            print(f"\n备份在：{BACKUP}")
            con.execute("PRAGMA foreign_keys = OFF")
            if con.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                print("✗ 外键没关掉——**不许继续**（DROP 会级联删掉会计的确认）")
                return 1
            if apply_to(con) != 0:
                return 1
            con.close()
            # 后置自检换一条**正常连接**跑：它自带外键开启与入口自检，
            # 用它读一遍才说明这个库现在是好的。
            from app.db.session import connect

            print("✓ 表已重建，后置自检…")
            normal = connect(DB)
            try:
                rc = verify(normal, before)
            finally:
                normal.close()
            if rc != 0:
                print("\n✗ 自检没过。**用备份回滚**：")
                print(f'    cp "{BACKUP}" "{DB}"')
                return rc
            print("✓ 自检全过（行数、外键、新值可插、假值仍被拒）")
            return 0
    finally:
        con.close()

    print(f"\n下一步：python scripts/verify_db.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
