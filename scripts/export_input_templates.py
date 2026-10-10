"""导出三张人工录入模板（CSV），以及把填好的 CSV 导回数据库。

用法：
    python scripts/export_input_templates.py --export   # 生成三个 CSV
    python scripts/export_input_templates.py --check    # 只校验 CSV 有没有填错
    python scripts/export_input_templates.py --import   # 填好的 CSV 写回库
    python scripts/export_input_templates.py --rekey    # 编号对不上时按原文重新编号

    # 只发/只收**一家**的表时必须加 --project，理由见下面「只处理一家」那一段
    python scripts/export_input_templates.py --export --project p-000959
    python scripts/export_input_templates.py --import --project p-000959

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

## ⚠ 编号会对不上，而且比文档里写的更容易发生

`claim_id` 是 `sha1(项目|章节|句序|归一化文本)`。**文本是稳的，
章节编号和句序不稳**：分段规则一改（比如「表格行伪装成正文」那几处过滤）、
或者重新解析一遍年报，章节序号就会平移，**后面所有 id 跟着变**。

模块 docstring 原来把这件事写成「确定性 id，防重跑」，那是对的——
同一份数据重跑确实一样。但**跨一次解析改动就不一样了**，
而这正好发生在「表格已经发给会计」之后：实测重建一次库，
会计交回来的 121 行**一行都对不上**。

所以有了 `--rekey`：编号虽然变了，**原文没变**。
它按归一化后的原文把旧编号换成新编号，逐条报出换了哪些、
哪些换不了（原文重复或找不到）。换不了的一律不猜。

> 这一条是 2026-10-06 真撞出来的：为了加 `claim_match` 的几列跑了
> `init_db.py --force`，会计填好的 121 行全部对不上。
> 幸好原文可用，185 行全部唯一命中，才没白填。

## ⚠ 只处理一家：`--project`

三张表平时是**三家混在一个 CSV 里**的，而 `--import` 会把**每一行**都写进去
（包括还是 `pending` 的新行）。所以给会计发「只有首钢」的那一份、
收回来直接 `--import`，会**把已经出分的宝钢和华菱一起踢出分**——
它们各有 1 条新主张还没人判，导进去 P 就不完整了。

2026-10-09 实测撞过。当时的做法是手工把 CSV 换成只含一家的再导，
但那**靠人记得还原**：漏一步就是两家的分没了，而且不报错。

所以做成开关：**发出去带 `--project`，收回来也必须带同一个**。
不带时它会照旧处理全部行——那是历史行为，别改。
"""

from __future__ import annotations

import argparse
import csv
import re
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
    # ⚠ **`project_id` 必须有。** 这一份导出**不带项目过滤**（三家公司混在一个
    #   文件里），而 `claim_id` 是 `cl-` + 哈希，看不出属于谁。
    #   2026-10-06 赵雨洁就是因此把整份表原样退回的——原话：
    #   「全部主张编号均为 `cl-...`，与说明中"只有 p-000932 开头的 26 条是华菱"
    #     不一致，因而无法可靠定位应复核的 26 条」。
    #   **她判断得对，是导出漏了一列。**
    ("project_id", "项目编号（只读）——`p-600019` 宝钢 / `p-000932` 华菱钢铁 / `p-000959` 首钢"),
    ("claim_id", "主张编号，不要改"),
    ("claim_text", "主张原文（只读，供你判断）"),
    ("source_page", "页码（只读）"),
    ("is_substantive", "1 是实质经营表述 / 0 不是（0 不进 P 分母）"),
    ("is_template", "1 模板化或回避 / 0 不是"),
    ("missing_elements", "缺哪些要素，用 | 分隔：object|period|metric|result|owner"),
    ("conclusion", "pending / p_penalty / no_penalty / needs_review"),
    ("reviewer", "复核人"),
    # ↓ 预判相关。**放在最后，且列名带「预判」**——一眼看得出这几列
    #   不是人填的，改起来也不会碰到前面那几列。
    ("预判", "← 这一行的前三列是模型预填的。**请注意复核，不要直接采信**"),
    ("预判理由", "模型给的依据（只读）"),
)

#: 预判标记列的取值。写的是模型名与提示词版本，便于回溯「这条是谁判的」。
PREDICTION_MARKER_EMPTY = ""

P_CONCLUSION = ("pending", "p_penalty", "no_penalty", "needs_review")


def main() -> int:
    parser = argparse.ArgumentParser(description="导出/导入人工录入模板")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--import", dest="do_import", action="store_true")
    group.add_argument("--rekey", action="store_true")
    parser.add_argument(
        "--project", default=None,
        help="只处理这一个项目（如 p-000959）。**发出去/收回来只有一家的表时必须加**，"
             "见下面那段说明。",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"✗ 数据库不存在：{DB_PATH}", file=sys.stderr)
        return 1

    con = connect(DB_PATH)
    try:
        if args.export:
            return _export(con, only_project=args.project)
        if args.rekey:
            return _rekey(con)
        return _check_or_import(
            con, do_import=args.do_import, only_project=args.project
        )
    finally:
        con.close()


# ---------------------------------------------------------------- 导出


def _export(con: sqlite3.Connection, *, only_project: str | None = None) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _export_q2(con, only_project=only_project)
    _export_r(con, only_project=only_project)
    _export_p(con, only_project=only_project)
    print()
    print(f"已生成到 {OUT_DIR}")
    print("把三个 CSV 发给会计同学，填好后用 --check 校验、--import 写回。")
    if only_project:
        print(
            f"⚠ 这一份**只含 {only_project}**。收回来的那份导入时也要带 "
            f"`--project {only_project}`——不带的话会把另外两家的候选行一起写进去，"
            f"而那两家已经出分了。"
        )
    return 0


def _write(
    path: Path, columns: tuple, rows: list[dict], *,
    only_project: str | None = None,
) -> None:
    """写 CSV。第二行是**说明行**——Excel 打开就能看到每一列该怎么填。"""
    if only_project:
        rows = [r for r in rows if r.get("project_id") == only_project]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([c for c, _ in columns])
        w.writerow([f"↑ {d}" for _, d in columns])   # 说明行
        for r in rows:
            w.writerow([r.get(c, "") for c, _ in columns])
    print(f"  {path.name:<24} {len(rows)} 行")


def _export_q2(con: sqlite3.Connection, *, only_project: str | None = None) -> None:
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
    _write(OUT_DIR / "Q2_账龄.csv", Q2_COLUMNS, rows,
           only_project=only_project)


def _export_r(con: sqlite3.Connection, *, only_project: str | None = None) -> None:
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
    _write(OUT_DIR / "R_风险检查.csv", R_COLUMNS, rows,
           only_project=only_project)


def _export_p(
    con: sqlite3.Connection, limit: int = 400, *,
    only_project: str | None = None,
) -> None:
    """P：候选主张清单。

    ⚠ **候选来源只有一个出口**：`app/skills/claim_scope.py`。那里同时管着
    按抽取器过滤和跨抽取器去重。这里若自己写一套 WHERE，
    就会和指数、判定表用的不是同一批主张——**而页面上两个数字都算得出来**，
    看不出它们不是一套。

    ⚠ **截断了要说出来。** 排序是 `project_id, claim_type, source_page`，
    超限时砍掉的是**末尾那一整块**（最后一家公司的后几个主题的全部主张）——
    不是「随机少一点」，是**整块丢**。不吭声的话，
    界面上表现为「某几类主张怎么一条都没确认过」，
    而谁都想不到是导出时被 LIMIT 掉了。

    ⚠ **这一份不带项目过滤**，三家公司混在一个文件里，所以
    `project_id` 那一列是**必须**的（见 `P_COLUMNS`）。
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
    predicted = _load_predictions(con)
    print(f"  预判：{len(predicted)} 条已预填（列名带「预判」，**复核后再采信**）")

    rows = []
    for r in con.execute(
        f"""
        SELECT c.project_id, c.claim_id, c.claim_text, c.source_page
        FROM claim c
        WHERE c.verifiable = 1{scope}
        ORDER BY c.project_id, c.claim_type, c.source_page
        LIMIT ?
        """,
        (*scope_params, limit),
    ):
        e = existing.get(r["claim_id"])
        p = predicted.get(r["claim_id"])
        rows.append(
            {
                "project_id": r["project_id"],
                "claim_id": r["claim_id"],
                # ⚠ 上限要**够长**。裁短了不只是「少几个字」：会计要判的是
                # 「这句话有没有对象 / 期间 / 指标 / 结果」，结果部分被砍掉，
                # 他会把一条完整的表述判成 missing_elements=result——
                # **判错的是我们造成的，而他看不出来**。
                # 实测最长的一句 326 字，取 400 就能全须全尾。
                "claim_text": (r["claim_text"] or "")[:400],
                "source_page": r["source_page"],
                # ⚠ 人填过的以**人为准**；没填过的才拿预判顶上。
                # 反过来的话，重导一次 CSV 就会把会计已经改过的值盖回去，
                # 而他会以为自己的修改没生效或者被系统否了。
                "is_substantive": e["is_substantive"] if e
                else (p["is_substantive"] if p else 1),
                "is_template": (e["is_template"] or 0) if e
                else (p["is_template"] if p and p["is_template"] is not None else ""),
                "missing_elements": (e["missing_elements"] or "") if e
                else (p["missing_elements"] if p else ""),
                # ★ **模型永远不填这一列。**
                # 会计口径：「模型只能提出候选，不能自动定 P」。P 的分子
                # 只数 p_penalty，填了就等于让模型定分。
                "conclusion": e["conclusion"] if e else "pending",
                "reviewer": e["reviewer"] if e else "",
                "预判": _marker(p),
                "预判理由": (p["reason"] or "") if p else "",
            }
        )
    _write(
        OUT_DIR / "P_人工确认.csv", P_COLUMNS, rows, only_project=only_project
    )


def _load_predictions(con: sqlite3.Connection) -> dict[str, dict]:
    """读模型预判。表不存在时返回空——**老库不带这张表**，
    而导出模板不该因为一个可选功能就跑不起来。"""
    try:
        return {
            r["claim_id"]: dict(r)
            for r in con.execute("SELECT * FROM p_prediction")
        }
    except sqlite3.OperationalError:
        return {}


def _marker(p: dict | None) -> str:
    """预判标记列。

    ⚠ 写的是**模型名 + 提示词版本**，不是一句「已预判」。会计和评委都可能
    回头问「这条是谁判的」——只写「是」，答案就查不到了。
    """
    if not p:
        return PREDICTION_MARKER_EMPTY
    model = str(p.get("model") or "?")
    version = str(p.get("prompt_version") or "?")
    return f"模型预填 {model}@{version}"


# ---------------------------------------------------------------- 校验 / 导入


def _read(path: Path) -> tuple[list[str], list[dict]]:
    """读 CSV，跳过第二行的说明行。

    ⚠ **必须先 `splitlines()` 再喂给 DictReader 是个静默数据损坏的 bug**
    （2026-10-10 修）。CSV 允许**引号字段里带换行**，而 `splitlines()`
    会把它切成两行——`DictReader` 于是把一条记录看成两条，
    **后面所有列整体错位**：

        p-000959,2015,demand_price,1,钢材下游需求…,"（风险章节「产品价格风险」p18）鉴于钢铁行业产能过剩、
        同质化竞争严重…",…,pending,,
                         ↑ 被切成两行，从 evidence 起全错位

    表现出来的样子是 `conclusion` 读成空串、校验报「取值 '' 不合法」，
    或者更糟——把 evidence 的半句当成 `mitigation` 写回库，
    **而它看起来就是一条填得好好的记录**。

    `io.StringIO` 交给 csv 自己按 RFC 4180 解析，引号内的换行才被正确对待。
    出处：R 的证据片段是从 PDF 正文里剪的，**天然带硬换行**。
    """
    import io

    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    return list(rows[0].keys() if rows else []), [
        r for r in rows if not any((v or "").startswith("↑") for v in r.values())
    ]


def _check_or_import(
    con: sqlite3.Connection, *, do_import: bool, only_project: str | None = None
) -> int:
    """校验并（可选）写回。

    ## `only_project`：只处理一家

    ⚠ **发出去只有一家的表时，收回来也必须只导那一家。** 三张表平时是三家混在
    一个 CSV 里的，而 `--import` 会把**每一行**都写进去（包括还是 `pending` 的新行）。
    所以直接导一份含三家的 CSV，**会把已经出分的宝钢和华菱一起踢出分**——
    它们的候选集里各有 1 条新主张还没人判，导进去 P 就不完整了。

    实测撞过一次（2026-10-09）：金标准做法是手工把 CSV 换成只含一家的再导，
    但那是**靠人记得还原**——漏一步就是两家的分没了，而且不报错。
    所以做成开关：`--project p-000959` 只处理该项目的行。
    """
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
        total = len(rows)
        if only_project:
            rows = [r for r in rows if (r.get("project_id") or "").strip() == only_project]
            print(
                f"  {name}：{total} 行 → 只取 {only_project} 的 {len(rows)} 行"
                f"（其余的**一律不碰**）"
            )
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
            # ⚠ 先别急着让人重发一份。**原文是稳的，只有编号会漂**——
            # 编号由「章节 + 句序 + 原文」哈希出来，分段规则一改、
            # 或者重解析一遍年报，章节序号就会平移，后面所有 id 跟着变。
            # 实测为加几列跑一次 `init_db.py --force`，会计填好的 121 行
            # 一行都对不上，而他们交回来的原句一个字没变。
            # 所以这里先试着按原文重新编号，能救回来就不用麻烦人。
            salvageable = _count_rekeyable(con, p_rows)
            hint = (
                f"**其中 {salvageable} 行的原文在库里能唯一定位，跑一次 "
                f"`--rekey` 就能把编号改过来，会计不用重填。**"
                if salvageable
                else "按原文也定位不了，只能重新 --export 一份发出去。"
            )
            problems.append(
                f"P_人工确认.csv：{len(orphan)}/{len(p_rows)} 条的 claim_id 在当前库里"
                f"不存在（例：{orphan[0]}）。**这批表的编号对不上库，填了也写不进去**"
                f"——CSV 是库的投影，重建库或重跑抽取都会让旧编号作废。{hint}"
            )

    _audit_predictions(con, p_rows)

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


def _audit_predictions(con: sqlite3.Connection, p_rows: list[dict]) -> None:
    """报出「有多少行原封不动地采信了模型预判」。

    ★ 这一条针对的是预判带来的**锚定风险**：会计看到已经有答案，容易直接放过。
    口径 §4.4 要的是「逐条人工确认」，如果退化成橡皮图章，P 这个分项的
    正当性就没了——而**从结果上完全看不出来**（值是对的，签名也有）。

    做法：拿 CSV 里的三列描述性判断去和 `p_prediction` 逐条比。
    一模一样 = 没改过。**只报数，不判对错**——改过的也未必更对，
    这里只是让「采信了多少」这件事有个数字。
    """
    try:
        predicted = {
            r["claim_id"]: dict(r) for r in con.execute("SELECT * FROM p_prediction")
        }
    except sqlite3.OperationalError:
        return
    if not predicted:
        return

    def _norm_elements(raw: str | None) -> str:
        return (raw or "").strip().replace("|", "").replace(",", "").replace(" ", "")

    same = 0
    reviewed = 0
    for row in p_rows:
        p = predicted.get((row.get("claim_id") or "").strip())
        if not p:
            continue
        # ⚠ 只统计**已经给了 conclusion 的行**。没给的行当然和预判一致
        # （原封没动过），把它们算进来会让「采信率」永远接近 100%，
        # 那个数字就没有信息量了。
        if (row.get("conclusion") or "pending").strip() == "pending":
            continue
        reviewed += 1
        if (
            str(row.get("is_substantive", "")).strip() == str(p["is_substantive"])
            and str(row.get("is_template", "")).strip()
            == ("" if p["is_template"] is None else str(p["is_template"]))
            and _norm_elements(row.get("missing_elements"))
            == _norm_elements(p["missing_elements"])
        ):
            same += 1

    # 一条 conclusion 都还没给时不出声：那时候「185 行全部一致」是必然的，
    # 报出来只会让人以为发现了什么。
    if not reviewed:
        return
    print(f"  预判采信情况：已给 conclusion 的 {reviewed} 行里，{same} 行的三列描述性判断与模型预判完全一致")
    if same >= reviewed * 0.9:
        print(
            "  ⚠ 一致度 ≥90%。口径 §4.4 要的是逐条人工确认——"
            "**采信本身不是错，但请确认每一条确实看过**。"
        )


# ---------------------------------------------------------------- 重新编号


def _norm_text(raw: str | None) -> str:
    """原文的归一化形式。和 `narrative._claim_id` 用的归一化保持一致——
    两处不一致的话，同一句话会算出两个键，重编号就永远命中不了。"""
    return " ".join((raw or "").split())


def _text_index(con: sqlite3.Connection) -> dict[str, list[str]]:
    """当前的 (归一化原文 → claim_id 列表)。**只收规则法**——
    P 表的候选只从它来，把模型法也收进来会让同一句话命中两条。"""
    by_text: dict[str, list[str]] = {}
    for cid, text in con.execute(
        "SELECT claim_id, claim_text FROM claim WHERE extractor LIKE 'rule:%'"
    ):
        by_text.setdefault(_norm_text(text), []).append(cid)
    return by_text


def _match_by_text(
    by_text: dict[str, list[str]], text: str
) -> tuple[str | None, str]:
    """按原文找当前编号。返回 (id 或 None, 说明)。

    ⚠ **只在唯一定位时才给 id。** 命中多条一律不猜——猜错的话那一行的判定
    会挂到别的主张上，而两行的值看起来都正常。

    CSV 里的原文截到 400 字，所以除了全等还要试前缀。
    """
    key = _norm_text(text)
    hit = by_text.get(key, [])
    if len(hit) == 1:
        return hit[0], "全等"
    if len(hit) > 1:
        return None, f"原文在库里对应 {len(hit)} 条，不猜"
    cands = sorted(
        {cid for full, ids in by_text.items() if key and full.startswith(key)
         for cid in ids}
    )
    if len(cands) == 1:
        return cands[0], "前缀"
    if len(cands) > 1:
        return None, f"前缀命中 {len(cands)} 条，不猜"
    return None, "库里找不到这句原文"


def _count_rekeyable(con: sqlite3.Connection, p_rows: list[dict]) -> int:
    """有多少行的原文在当前库里能唯一定位——**别让会计白重填一遍**。

    这一步只数数，不改任何东西：`--check` 是只读的。
    """
    known = {r[0] for r in con.execute("SELECT claim_id FROM claim")}
    by_text = _text_index(con)
    n = 0
    for row in p_rows:
        cid = (row.get("claim_id") or "").strip()
        if not cid or cid in known:
            continue
        if _match_by_text(by_text, row.get("claim_text", ""))[0]:
            n += 1
    return n


def _rekey(con: sqlite3.Connection) -> int:
    """按原文把 P 表里过期的 claim_id 换成当前的。

    改的是 `人工录入/P_人工确认.csv` 本身（原地重写），改完要再跑一次 `--check`。
    """
    path = OUT_DIR / "P_人工确认.csv"
    if not path.exists():
        print(f"✗ 找不到 {path}（先跑 --export）", file=sys.stderr)
        return 1

    fieldnames, rows = _read(path)
    known = {r[0] for r in con.execute("SELECT claim_id FROM claim")}
    by_text = _text_index(con)

    def _lookup(text: str) -> tuple[str | None, str]:
        return _match_by_text(by_text, text)

    changed, failed, skipped = [], [], 0
    dropped_ids: set[str] = set()
    dropped: list[tuple[str, str]] = []
    for row in rows:
        cid = (row.get("claim_id") or "").strip()
        if not cid or cid in known:
            skipped += 1
            continue
        new_id, how = _lookup(row.get("claim_text", ""))
        if new_id is None:
            # ★ **还是 `pending` 的行直接丢掉。**
            #
            # 它对应的主张已经不在库里了（规则一收紧，那句就不该是主张），
            # 按原文也定位不到，所以这一行**没有任何东西可挂**。
            # 留着的话会卡死整批导入——`--check` 是「整体拒绝」，
            # 一条对不上的 P 行会让**会计填好的 R 和 Q2 也一起进不来**。
            #
            # ⚠ 只丢 `pending` 的。**已经有结论的一律不丢**——那是人的活儿，
            #   哪怕暂时挂不上也要留下来让人看见（见下面的 failed）。
            if (row.get("conclusion") or "").strip() == "pending":
                dropped_ids.add(cid)
                dropped.append((cid, row.get("claim_text", "")[:60]))
                continue
            failed.append((cid, how))
            continue
        changed.append((cid, new_id, how))
        row["claim_id"] = new_id

    if not changed and not failed and not dropped:
        print(f"✓ 全部 {len(rows)} 行的编号都对得上，不用改")
        return 0

    if dropped:
        print(f"  · {len(dropped)} 行**已从表里删掉**：主张已不存在、按原文也定位不到，"
              f"且它们还是 `pending`（没有结论，丢了不损失任何人的判定）：")
        for cid, text in dropped:
            print(f"      {cid} | {text}")

    if failed:
        print(f"✗ {len(failed)} 行换不了，**这些行没有改动**：")
        for cid, why in failed[:20]:
            print(f"  · {cid}：{why}")
        print("  换不了就请会计重判这几行，或者人工核对后手工填 claim_id。")
        print("  **不要自己猜一个编号**——判定会挂到别的主张上，而两行看起来都正常。")

    # 丢掉的行要从**写回的内容**里去掉——只收集不删除的话，
    # 报告会说「已删掉 6 行」而文件里一行没少，接着 `--check` 照样卡住。
    kept = [r for r in rows if (r.get("claim_id") or "").strip() not in dropped_ids]
    _write_preserving(path, fieldnames, kept)
    print(f"✓ 已重编号 {len(changed)} 行、删掉 {len(dropped_ids)} 行"
          f"（原文件原地重写，{len(rows)} → {len(kept)}）")
    for old, new, how in changed[:10]:
        print(f"  · {old} → {new}   （{how}）")
    if len(changed) > 10:
        print(f"  …还有 {len(changed) - 10} 行")
    if skipped:
        print(f"  另有 {skipped} 行编号本来就是对的，未改动")
    print("  请再跑一次 --check 确认；确认无误后 --import 写回。")
    return 1 if failed else 0


def _write_preserving(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """原地重写 CSV，**保留说明行与 BOM**。

    用 `_write` 会把说明行丢掉——会计下次打开会看到一列没头没脑的英文列名。
    """
    text = path.read_text(encoding="utf-8-sig").splitlines()
    reader = csv.DictReader(text)
    desc_row = next(
        (r for r in reader if any((v or "").startswith("↑") for v in r.values())),
        None,
    )
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(fieldnames)
        if desc_row:
            w.writerow([desc_row.get(c, "") for c in fieldnames])
        for r in rows:
            w.writerow([r.get(c, "") for c in fieldnames])


#: 页码：单个页 `113`、跨页 `113-114`（全角连字符也收），
#: 后面可以再跟一句括注，如 `130（宝钢2016年报）`。
#:
#: ⚠ 两种尾巴都是**真实填过的**，而且都带信息：
#:
#:   · 跨页（`113-114`）——宝钢 2015/2016/2018/2022、华菱 2022 的账龄表都跨两页。
#:     这曾经是一个「校验放过、导入崩溃」的缺口：`_check_q2` 压根没看
#:     `source_page`，而 `_import` 里是裸的 `int(...)`，于是 5 行 `113-114`
#:     一路通过校验、最后抛 `ValueError`——会计看到的是「校验通过」
#:     紧跟一堆报错，而错误信息里没有一个字说得出是哪一列。
#:   · 括注（`130（宝钢2016年报）`）——**这一页在另一份年报里**。
#:     宝钢 2015 的全口径账龄是印在 2016 年报的「年初余额」列里的，
#:     不把这个说明留住，复核的人拿 2015 年报翻到 130 页会什么都找不到。
#:   · 多页（`21,27-28`）——风险那几项的依据分散在好几页上。
#:     入库取**第一页**（证据链的落点），整串写进 note。
#:     这一条是 2026-10-06 赵雨洁交回华菱 R 表时才补的：她写的是
#:     `21,27-28`，而当时只认单页和区间，于是**两张表都卡在校验上、
#:     一行都导不进去**——她填得没错，是我们的格式太窄。
_PAGE_ITEM = r"\d{1,4}(\s*[-–—]\s*\d{1,4})?"
_PAGE_RE = re.compile(
    rf"^{_PAGE_ITEM}(\s*[、,，]\s*{_PAGE_ITEM})*(\s*[（(].*[）)])?$"
)


def _page_value(raw: str) -> tuple[int | None, str | None]:
    """页码 → `(入库的整数页, 要补进 note 的说明)`。

    跨页时入库的是**起始页**（证据链点过去落在账龄表的第一页），
    而「这张表还印到了哪一页」写进 note——**不能只留起始页就完事**：
    复核的人按起始页翻过去，会发现「1 年以上」那几行其实在下一页。
    括注同理：它说的是「这一页在哪份年报里」，丢了就找不到原件。
    多页（`21,27-28`）同样取第一页，整串进 note。
    """
    v = (raw or "").strip()
    if not v:
        return None, None
    # 多页：逗号/顿号分开的一串。第一项是入库的落点，整串进 note。
    if re.match(rf"^{_PAGE_ITEM}(\s*[、,，]\s*{_PAGE_ITEM})+", v):
        first = re.match(rf"^({_PAGE_ITEM})", v).group(1)
        start = re.match(r"^\d{1,4}", first).group(0)
        return int(start), f"依据分散在多页：{v}"
    m = re.match(r"^(\d{1,4})\s*[-–—]\s*(\d{1,4})\s*(.*)$", v)
    if m:
        note = f"该表跨页：{m.group(1)}–{m.group(2)}"
        if m.group(3).strip():
            note += f"；{m.group(3).strip()}"
        return int(m.group(1)), note
    m = re.match(r"^(\d{1,4})\s*(.*)$", v)
    if m:
        tail = m.group(2).strip()
        return int(m.group(1)), (f"来源说明：{tail}" if tail else None)
    return None, None


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
    # 页码格式。**这一条的来历**：会计填了 5 行跨页（`113-114`），
    # 校验一声不响地放过去，直到写库才崩在 `int('113-114')` 上。
    page = (row.get("source_page") or "").strip()
    if page and not _PAGE_RE.match(page):
        out.append(
            f"source_page 取值 {page!r} 认不出来，只能填单个页码（`113`）"
            f"或跨页区间（`113-114`）"
        )
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
    # 页码格式同 Q2：`int()` 崩在写库那一步、校验却放过去，
    # 会计看到的是「校验通过」紧跟着一个 traceback。见 `_PAGE_RE` 的说明。
    page = (row.get("source_page") or "").strip()
    if page and not _PAGE_RE.match(page):
        out.append(
            f"source_page 取值 {page!r} 认不出来，只能填单个页码（`113`）"
            f"或跨页区间（`113-114`）"
        )
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
            page, page_note = _page_value(row.get("source_page", ""))
            note = row.get("note") or None
            if page_note:
                # 跨页说明**追加**而不是覆盖：会计自己写的 note 不能丢。
                note = f"{note}；{page_note}" if note else page_note
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
                    page,
                    row.get("source_text") or None,
                    (row.get("status") or "pending").strip(),
                    note,
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
                    _page_value(row.get("source_page", ""))[0],
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
