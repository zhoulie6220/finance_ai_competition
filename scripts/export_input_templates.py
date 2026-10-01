"""导出三张人工录入模板（CSV），以及把填好的 CSV 导回数据库。

用法：
    python scripts/export_input_templates.py --export   # 生成三个 CSV
    python scripts/export_input_templates.py --check    # 只校验 CSV 有没有填错
    python scripts/export_input_templates.py --import   # 填好的 CSV 写回库

生成到 `人工录入/` 目录下：

    Q2_账龄.csv          每个公司每个年度一行
    R_风险检查.csv        每个公司每个年度四项
    P_人工确认.csv        每条候选主张一行

## 为什么要用 CSV 而不是直接改数据库

会计同学不写代码。CSV 能用 Excel 打开、能标注、能发回群里，
而且**填错了当场能看出来**——直接改库的话，字段名对不上、枚举写错、
或者把「未找到」填成 0，都不会报错，只会静默算出一个错的结果。

## 枚举值是**封闭的**

CSV 里每一列的合法取值都写在表头注释里，`--check` 会逐格校验。
写错一个值不会让它「差不多能用」——`--import` 会**整体拒绝**，
并告诉你是哪一行哪一列。
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
# _console 在 backend/scripts/ 下，两个脚本共用同一份编码兜底。
# 复制一份到这里的话，改了一处忘了另一处，两边都不会报错。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "scripts"))

# _console 与本文件同目录。**这一行不能省**：
# 直接 `python scripts/x.py` 时 Python 会自动把脚本目录放进 sys.path，
# 但测试用 `spec_from_file_location` 按路径加载脚本时**不会**——
# 少了它，`import _console` 只在跑测试时炸，看起来像测试坏了。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402  (与本文件同目录)

_console.setup()

from app.db.session import connect  # noqa: E402
from app.engine.attestation import RISK_ITEMS  # noqa: E402
from app.skills import claim_scope  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
DB_PATH = BACKEND_DIR / "var" / "finance.db"
OUT_DIR = Path(__file__).resolve().parent.parent / "人工录入"

# ---------------------------------------------------------------- 列定义

#: 每一列：(列名, 说明)。说明会写成 CSV 的第二行注释，Excel 里看得见。
Q2_COLUMNS = (
    ("project_id", "项目编号，不要改"),
    ("period", "年度，不要改"),
    ("scope", "口径，固定 consolidated（会计口径 §2.1 要求合并报表）"),
    ("receivable_gross", "应收账款**账面余额**（gross），单位：元"),
    ("over_one_year", "账龄「1年以上」的账面余额，单位：元"),
    ("revenue", "营业收入，单位：元"),
    ("source_page", "年报页码"),
    ("source_text", "原文片段（那几行的原文，便于复核）"),
    ("status", "pending / validated / proxy_net / unavailable_disclosure / needs_review"),
    ("note", "非 validated 时必填：为什么"),
    ("reviewer", "复核人"),
)

Q2_STATUS = ("pending", "validated", "proxy_net", "unavailable_disclosure", "needs_review")

R_COLUMNS = (
    ("project_id", "项目编号，不要改"),
    ("period", "年度，不要改"),
    ("item", "四项之一，不要改"),
    ("applicable", "1 适用 / 0 不适用（不适用必须在 evidence 里写业务范围证据）"),
    ("risk_object", "风险对象"),
    ("impact_path", "作用路径：对收入/成本/现金流/产能/估值的影响"),
    ("evidence", "可核验的指标、金额、期间、事件或管理措施"),
    ("mitigation", "缓释措施（可空）"),
    ("conclusion", "pending / sufficient / insufficient / not_applicable / needs_review"),
    ("source_page", "年报页码"),
    ("reviewer", "复核人"),
)

R_CONCLUSION = ("pending", "sufficient", "insufficient", "not_applicable", "needs_review")

P_COLUMNS = (
    ("claim_id", "主张编号，不要改"),
    ("claim_text", "主张原文（只读，供你判断）"),
    ("source_page", "页码（只读）"),
    ("is_substantive", "1 是实质经营表述 / 0 不是（0 不进 P 分母）"),
    ("is_template", "1 模板化或回避 / 0 不是"),
    ("missing_elements", "缺哪些要素，用 | 分隔：object|period|metric|result|owner"),
    ("conclusion", "pending / p_penalty / no_penalty / needs_review"),
    ("reviewer", "复核人"),
)

P_CONCLUSION = ("pending", "p_penalty", "no_penalty", "needs_review")


def main() -> int:
    parser = argparse.ArgumentParser(description="导出/导入人工录入模板")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--import", dest="do_import", action="store_true")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"✗ 数据库不存在：{DB_PATH}", file=sys.stderr)
        return 1

    con = connect(DB_PATH)
    try:
        if args.export:
            return _export(con)
        return _check_or_import(con, do_import=args.do_import)
    finally:
        con.close()


# ---------------------------------------------------------------- 导出


def _export(con: sqlite3.Connection) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _export_q2(con)
    _export_r(con)
    _export_p(con)
    print()
    print(f"已生成到 {OUT_DIR}")
    print("把三个 CSV 发给会计同学，填好后用 --check 校验、--import 写回。")
    return 0


def _write(path: Path, columns: tuple, rows: list[dict]) -> None:
    """写 CSV。第二行是**说明行**——Excel 打开就能看到每一列该怎么填。"""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([c for c, _ in columns])
        w.writerow([f"↑ {d}" for _, d in columns])   # 说明行
        for r in rows:
            w.writerow([r.get(c, "") for c, _ in columns])
    print(f"  {path.name:<24} {len(rows)} 行")


def _export_q2(con: sqlite3.Connection) -> None:
    """Q2：每个公司每个年度一行，先把已有记录带出来，没有的留空。"""
    rows = []
    existing = {
        (r["project_id"], r["period"], r["scope"]): r
        for r in con.execute("SELECT * FROM q2_aging")
    }
    for project_id, period, scope in con.execute(
        """
        SELECT p.project_id, f.period, p.base_scope
        FROM project p JOIN file f ON f.project_id = p.project_id
        WHERE f.role = 'annual_report'
        GROUP BY p.project_id, f.period, p.base_scope
        ORDER BY p.project_id, f.period
        """
    ):
        key = (project_id, period, scope)
        r = existing.get(key)
        rows.append(
            {
                "project_id": project_id,
                "period": period,
                "scope": scope,
                "receivable_gross": r["receivable_gross"] if r else "",
                "over_one_year": r["over_one_year"] if r else "",
                "revenue": r["revenue"] if r else "",
                "source_page": r["source_page"] if r else "",
                "source_text": r["source_text"] if r else "",
                "status": (r["status"] if r else "pending"),
                "note": r["note"] if r else "",
                "reviewer": r["reviewer"] if r else "",
            }
        )
    _write(OUT_DIR / "Q2_账龄.csv", Q2_COLUMNS, rows)


def _export_r(con: sqlite3.Connection) -> None:
    """R：每个公司每个年度 × 四项。"""
    rows = []
    existing = {
        (r["project_id"], r["period"], r["item"]): r
        for r in con.execute("SELECT * FROM risk_disclosure_check")
    }
    for project_id, period in con.execute(
        "SELECT p.project_id, f.period FROM project p"
        " JOIN file f ON f.project_id = p.project_id"
        " WHERE f.role = 'annual_report'"
        " GROUP BY p.project_id, f.period ORDER BY p.project_id, f.period"
    ):
        for item, _label in RISK_ITEMS:
            r = existing.get((project_id, period, item))
            rows.append(
                {
                    "project_id": project_id,
                    "period": period,
                    "item": item,
                    "applicable": (r["applicable"] if r else 1),
                    "risk_object": r["risk_object"] if r else "",
                    "impact_path": r["impact_path"] if r else "",
                    "evidence": r["evidence"] if r else "",
                    "mitigation": r["mitigation"] if r else "",
                    "conclusion": (r["conclusion"] if r else "pending"),
                    "source_page": r["source_page"] if r else "",
                    "reviewer": r["reviewer"] if r else "",
                }
            )
    _write(OUT_DIR / "R_风险检查.csv", R_COLUMNS, rows)


def _export_p(con: sqlite3.Connection, limit: int = 400) -> None:
    """P：候选主张清单。

    ⚠ **候选来源只有一个出口**：`app/skills/claim_scope.py`。那里同时管着
    按抽取器过滤和跨抽取器去重。这里若自己写一套 WHERE，
    就会和指数、判定表用的不是同一批主张——**而页面上两个数字都算得出来**，
    看不出它们不是一套。

    ⚠ **截断了要说出来。** 排序是 `claim_type, source_page`，超限时砍掉的是
    **排序靠后的那几个主题的全部主张**——不是「随机少一点」，是**按主题整块丢**。
    不吭声的话，界面上表现为「某几类主张怎么一条都没确认过」，
    而谁都想不到是导出时被 LIMIT 掉了。
    """
    scope, scope_params = claim_scope.scope_sql("c")
    total = con.execute(
        f"SELECT COUNT(*) FROM claim c WHERE c.verifiable = 1{scope}",
        scope_params,
    ).fetchone()[0]
    if total > limit:
        print(
            f"  ⚠ P 表候选共 {total} 条，只导出前 {limit} 条（按主题排序截断）。"
            f"**剩下的 {total - limit} 条这次默认「无确认记录」**——"
            f"要全导请在 _export_p 里调大 limit。"
        )
    print(f"  {claim_scope.describe()}")
    existing = {
        r["claim_id"]: r for r in con.execute("SELECT * FROM p_confirmation")
    }
    rows = []
    for r in con.execute(
        f"""
        SELECT c.claim_id, c.claim_text, c.source_page
        FROM claim c
        WHERE c.verifiable = 1{scope}
        ORDER BY c.claim_type, c.source_page
        LIMIT ?
        """,
        (*scope_params, limit),
    ):
        e = existing.get(r["claim_id"])
        rows.append(
            {
                "claim_id": r["claim_id"],
                # ⚠ 上限要**够长**。裁短了不只是「少几个字」：会计要判的是
                # 「这句话有没有对象 / 期间 / 指标 / 结果」，结果部分被砍掉，
                # 他会把一条完整的表述判成 missing_elements=result——
                # **判错的是我们造成的，而他看不出来**。
                # 实测最长的一句 326 字，取 400 就能全须全尾。
                "claim_text": (r["claim_text"] or "")[:400],
                "source_page": r["source_page"],
                "is_substantive": e["is_substantive"] if e else 1,
                "is_template": "" if not e else (e["is_template"] or 0),
                "missing_elements": e["missing_elements"] if e else "",
                "conclusion": e["conclusion"] if e else "pending",
                "reviewer": e["reviewer"] if e else "",
            }
        )
    _write(OUT_DIR / "P_人工确认.csv", P_COLUMNS, rows)


# ---------------------------------------------------------------- 校验 / 导入


def _read(path: Path) -> tuple[list[str], list[dict]]:
    """读 CSV，跳过第二行的说明行。"""
    text = path.read_text(encoding="utf-8-sig").splitlines()
    reader = csv.DictReader(text)
    rows = list(reader)
    # 第二行是说明行（每格以 ↑ 开头），丢掉
    return list(reader.fieldnames or []), [
        r for r in rows if not any((v or "").startswith("↑") for v in r.values())
    ]


def _check_or_import(con: sqlite3.Connection, *, do_import: bool) -> int:
    problems: list[str] = []
    parsed: dict[str, list[dict]] = {}

    for name, columns, checker in (
        ("Q2_账龄.csv", Q2_COLUMNS, _check_q2),
        ("R_风险检查.csv", R_COLUMNS, _check_r),
        ("P_人工确认.csv", P_COLUMNS, _check_p),
    ):
        path = OUT_DIR / name
        if not path.exists():
            problems.append(f"{name}：文件不存在（先跑 --export）")
            continue
        _, rows = _read(path)
        parsed[name] = rows
        for i, row in enumerate(rows, start=3):     # 表头 1 行 + 说明 1 行
            problems.extend(f"{name} 第 {i} 行：{p}" for p in checker(row))

    # ⚠ 逐条核对 P 表的 claim_id 在不在**当前这个库**里。
    #
    # 不核的话，`_import` 会把对不上的行**静默 continue 掉**——会计填了几百行，
    # 脚本打印「✓ 已写回 0 行」，而 P 项照样算不出来。**没有任何一处报错。**
    #
    # 这件事真的发生过：`人工录入/P_人工确认.csv` 是照着另一个库导出的，
    # 换台机器重跑抽取之后，400 条的 id 与库里 1047 条**零重合**。
    # CSV 是库的投影，库一变（重建、重跑抽取、换 prompt 版本），投影就作废。
    p_rows = parsed.get("P_人工确认.csv") or []
    if p_rows:
        known = {r[0] for r in con.execute("SELECT claim_id FROM claim")}
        orphan = [r.get("claim_id", "") for r in p_rows if r.get("claim_id") not in known]
        if orphan:
            problems.append(
                f"P_人工确认.csv：{len(orphan)}/{len(p_rows)} 条的 claim_id 在当前库里"
                f"不存在（例：{orphan[0]}）。**这批表对不上库，填了也写不进去**——"
                "CSV 是库的投影，重建库或重跑抽取都会让旧 CSV 作废。"
                "请先确认库与 CSV 出自同一份数据，再重新 --export 一份发出去。"
            )

    if problems:
        print(f"✗ 发现 {len(problems)} 处问题，**整体拒绝**（会计口径 §七：不猜、不填 0）：")
        for p in problems[:25]:
            print(f"  · {p}")
        if len(problems) > 25:
            print(f"  …还有 {len(problems) - 25} 处")
        return 1

    print(f"✓ 三份 CSV 校验通过")
    if not do_import:
        print("  要写回数据库请加 --import")
        return 0

    n, p_dropped = _import(con, parsed)
    print(f"✓ 已写回 {n} 行")
    if p_dropped:
        # 走到这里说明校验那一步被绕过了（比如直接调 _import），
        # 仍然要出声——**静默丢数据是这个文件最不该有的行为**。
        print(
            f"✗ 另有 {p_dropped} 行 P 表因为 claim_id 不在库里被丢弃，"
            "**会计的判定没有生效**。先跑 --check 看是哪些。",
            file=sys.stderr,
        )
        return 1
    return 0


def _num(raw: str, field: str) -> str | None:
    """校验数字列。**不允许把「未找到」填成 0**——那是 §七 的禁止事项。"""
    v = (raw or "").strip()
    if not v:
        return None
    try:
        float(v.replace(",", ""))
    except ValueError:
        raise ValueError(f"{field} 不是数字：{v!r}")
    return v.replace(",", "")


def _check_q2(row: dict) -> list[str]:
    out: list[str] = []
    status = (row.get("status") or "").strip()
    if status not in Q2_STATUS:
        return [f"status 取值 {status!r} 不合法，只能是 {'/'.join(Q2_STATUS)}"]

    if status == "validated":
        for col in ("receivable_gross", "over_one_year", "revenue"):
            try:
                if _num(row.get(col, ""), col) is None:
                    out.append(f"status=validated 但 {col} 是空的")
            except ValueError as e:
                out.append(str(e))
    if status in ("unavailable_disclosure", "needs_review") and not (row.get("note") or "").strip():
        out.append(f"status={status} 必须填 note 说明原因")
    return out


def _check_r(row: dict) -> list[str]:
    out: list[str] = []
    conclusion = (row.get("conclusion") or "").strip()
    if conclusion not in R_CONCLUSION:
        return [f"conclusion 取值 {conclusion!r} 不合法"]

    if conclusion == "sufficient":
        for col in ("risk_object", "impact_path", "evidence"):
            if not (row.get(col) or "").strip():
                out.append(f"判为 sufficient 但 {col} 是空的（三项要素缺一不可）")
    if conclusion == "not_applicable" and not (row.get("evidence") or "").strip():
        out.append("判为 not_applicable 必须在 evidence 里写业务范围证据")
    return out


def _check_p(row: dict) -> list[str]:
    out: list[str] = []
    conclusion = (row.get("conclusion") or "").strip()
    if conclusion not in P_CONCLUSION:
        return [f"conclusion 取值 {conclusion!r} 不合法"]
    if conclusion != "pending" and not (row.get("reviewer") or "").strip():
        out.append("非 pending 的判定必须填 reviewer")
    return out


def _import(con: sqlite3.Connection, parsed: dict[str, list[dict]]) -> tuple[int, int]:
    """写回。整批一个事务，中途失败整体回滚。

    返回 `(写入行数, 丢掉的 P 行数)`。

    ⚠ **第二个数不能吞。** 原来这里对找不到的 `claim_id` 直接 `continue`，
    于是「会计填了 400 行、一行都没写进去」与「填了 400 行、全部写进去了」
    在输出上**长得一模一样**。现在把它报出来——调用方会在非零时给醒目提示。
    """
    now = _utc_now()
    n = 0
    p_dropped = 0
    with con:
        for row in parsed.get("Q2_账龄.csv", []):
            con.execute(
                "INSERT INTO q2_aging (id, project_id, period, scope,"
                " receivable_gross, over_one_year, revenue, source_page,"
                " source_text, status, note, reviewer, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project_id, period, scope) DO UPDATE SET"
                " receivable_gross=excluded.receivable_gross,"
                " over_one_year=excluded.over_one_year,"
                " revenue=excluded.revenue, source_page=excluded.source_page,"
                " source_text=excluded.source_text, status=excluded.status,"
                " note=excluded.note, reviewer=excluded.reviewer",
                (
                    f"q2-{row['project_id']}-{row['period']}-{row['scope']}",
                    row["project_id"], row["period"], row["scope"],
                    _num(row.get("receivable_gross", ""), "receivable_gross"),
                    _num(row.get("over_one_year", ""), "over_one_year"),
                    _num(row.get("revenue", ""), "revenue"),
                    int(row["source_page"]) if (row.get("source_page") or "").strip() else None,
                    row.get("source_text") or None,
                    (row.get("status") or "pending").strip(),
                    row.get("note") or None,
                    row.get("reviewer") or None,
                    now,
                ),
            )
            n += 1
        for row in parsed.get("R_风险检查.csv", []):
            con.execute(
                "INSERT INTO risk_disclosure_check (id, project_id, period, item,"
                " applicable, risk_object, impact_path, evidence, mitigation,"
                " conclusion, source_page, reviewer, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(project_id, period, item) DO UPDATE SET"
                " applicable=excluded.applicable, risk_object=excluded.risk_object,"
                " impact_path=excluded.impact_path, evidence=excluded.evidence,"
                " mitigation=excluded.mitigation, conclusion=excluded.conclusion,"
                " source_page=excluded.source_page, reviewer=excluded.reviewer",
                (
                    f"risk-{row['project_id']}-{row['period']}-{row['item']}",
                    row["project_id"], row["period"], row["item"],
                    1 if str(row.get("applicable", "1")).strip() == "1" else 0,
                    row.get("risk_object") or None,
                    row.get("impact_path") or None,
                    row.get("evidence") or None,
                    row.get("mitigation") or None,
                    (row.get("conclusion") or "pending").strip(),
                    int(row["source_page"]) if (row.get("source_page") or "").strip() else None,
                    row.get("reviewer") or None,
                    now,
                ),
            )
            n += 1
        for row in parsed.get("P_人工确认.csv", []):
            claim_id = (row.get("claim_id") or "").strip()
            if not claim_id:
                continue
            exists = con.execute(
                "SELECT 1 FROM claim WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            if not exists:
                p_dropped += 1
                continue
            con.execute(
                "INSERT INTO p_confirmation (id, claim_id, is_substantive,"
                " is_template, missing_elements, conclusion, reviewer, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(claim_id) DO UPDATE SET"
                " is_substantive=excluded.is_substantive,"
                " is_template=excluded.is_template,"
                " missing_elements=excluded.missing_elements,"
                " conclusion=excluded.conclusion, reviewer=excluded.reviewer",
                (
                    f"p-{claim_id}", claim_id,
                    1 if str(row.get("is_substantive", "1")).strip() == "1" else 0,
                    None if not (row.get("is_template") or "").strip()
                    else (1 if str(row["is_template"]).strip() == "1" else 0),
                    _json_list(row.get("missing_elements")),
                    (row.get("conclusion") or "pending").strip(),
                    row.get("reviewer") or None,
                    now,
                ),
            )
            n += 1
    return n, p_dropped


def _json_list(raw: str | None) -> str:
    import json

    if not raw or not raw.strip():
        return "[]"
    parts = [p.strip() for p in raw.replace(";", "|").split("|") if p.strip()]
    return json.dumps(parts, ensure_ascii=False)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
