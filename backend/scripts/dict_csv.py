"""字段字典的 CSV 往返工具。

字段字典由会计/金融方向同学负责，但他们不该去手改 SQL 里嵌的 JSON 数组：

    ('revenue', '营业收入', '["营业收入","主营业务收入"]', ...

中括号、逗号、引号全挤在一行，改错一个引号**不会报错**，只会让那个字段静默失效。
所以字典以 CSV 作为人机界面，SQL 作为机器真源，两者由本工具保持同步。

用法：
    python scripts/dict_csv.py --export      # SQL → CSV，交给会计同学编辑
    python scripts/dict_csv.py --import      # CSV → SQL，写入前先校验
    python scripts/dict_csv.py --check       # 只校验两者是否同步（可放进 CI）

导入是**先校验、后写入**：候选 SQL 会先在内存库里加载并跑一遍
`app.db.dictionary.validate()`，有任何一条不通过就整体拒绝，原文件不动。
这样「改坏了一半」不会落盘，也不会出现建库失败才发现问题的情况。

多值列（别名、排除词）在 CSV 里用 `|` 分隔，导入时也接受 `;` 与 `；`。
"""

from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _console  # noqa: E402  (与本文件同目录)

_console.setup()

from app.db import dictionary  # noqa: E402
from app.db.session import BACKEND_DIR, connect_memory, init_schema, load_seeds  # noqa: E402
from app.schemas.enums import SignConvention, Statement, UnitKind, ValueType  # noqa: E402

SEED_PATH = BACKEND_DIR.joinpath(*dictionary.SEED_PATH_PARTS)
CSV_PATH = BACKEND_DIR / "app" / "data" / "metric_dictionary.csv"

# 单元格里塞得下的文本长度上限，纯提示用
_WIDE_COLUMNS = {"aliases", "exclusion_terms", "note", "example_sentence", "scope_note"}


def _memory_with_seed(seed_text: str | None = None) -> sqlite3.Connection:
    """建一个装好 schema 的内存库。

    `seed_text` 为空时加载磁盘上的种子文件；否则只执行传入的这段 SQL，
    用于「先试装候选内容」。
    """
    con = connect_memory()
    init_schema(con)
    if seed_text is None:
        load_seeds(con)
    else:
        con.executescript(seed_text)
    return con


def _read_seed_text() -> str:
    if not SEED_PATH.exists():
        raise SystemExit(f"找不到种子文件：{SEED_PATH}")
    return SEED_PATH.read_text(encoding="utf-8")


def cmd_export(csv_path: Path) -> int:
    con = _memory_with_seed()
    rows = dictionary.fetch(con)
    con.close()

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(dictionary.COLUMNS))
        writer.writeheader()
        for row in rows:
            out = {}
            for col in dictionary.COLUMNS:
                value = row.get(col)
                if col in dictionary.JSON_COLUMNS:
                    value = dictionary.MULTI_SEP.join(dictionary._parse_multi(value))
                out[col] = "" if value is None else value
            writer.writerow(out)

    print(f"已导出 {len(rows)} 个字段 → {csv_path}")
    print("多值列（aliases / exclusion_terms）用 | 分隔，一格一个，不必碰中括号和引号。")
    return 0


def _read_csv(csv_path: Path) -> tuple[list[dict], list[str]]:
    """读 CSV。返回 (行列表, 问题列表)。"""
    if not csv_path.exists():
        raise SystemExit(f"找不到 CSV：{csv_path}")

    text = csv_path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise SystemExit(f"CSV 是空的：{csv_path}")

    # Excel 在中文环境下另存时可能把分隔符改成分号，嗅探一下免得整份读崩
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header = reader.fieldnames or []
    missing = [c for c in dictionary.COLUMNS if c not in header]
    if missing:
        return [], [
            f"CSV 缺少列：{'、'.join(missing)}。"
            f"请先用 python scripts/dict_csv.py --export 生成模板，不要手工建表头。"
        ]

    rows: list[dict] = []
    for raw in reader:
        if not (raw.get("metric_key") or "").strip():
            continue  # Excel 里常见的空尾行
        rows.append({c: (raw.get(c) or "").strip() for c in dictionary.COLUMNS})
    return rows, []


def _preflight(rows: list[dict]) -> list[str]:
    """装库**之前**按行检查。

    这些错误数据库也能拦（外键、CHECK），但报出来的是
    `FOREIGN KEY constraint failed` 之类——对着 89 行的 Excel 表，
    没人知道该去看哪一行。所以在这里先查一遍，把 Excel 的行号和字段键带上。
    """
    problems: list[str] = []
    keys = {r["metric_key"] for r in rows}

    for i, r in enumerate(rows, start=2):  # 第 1 行是表头
        where = f"第 {i} 行 [{r['metric_key']}]"

        for col, allowed in (
            ("statement", {s.value for s in Statement}),
            ("value_type", {v.value for v in ValueType}),
            ("unit_kind", {u.value for u in UnitKind}),
            ("sign_convention", {s.value for s in SignConvention}),
        ):
            if r.get(col) not in allowed:
                problems.append(
                    f"{where} {col}={r.get(col)!r} 不是允许的取值，"
                    f"只能是：{'、'.join(sorted(allowed))}"
                )

        for col in ("is_nonrecurring", "is_derived"):
            text = str(r.get(col) or "").strip().lower()
            if text not in ("", "0", "1", "true", "false", "no", "yes", "n", "y", "是", "否"):
                problems.append(f"{where} {col}={r.get(col)!r} 只能填 0 或 1")

        order = r.get("display_order") or ""
        if str(order).strip() and _to_int(order) is None:
            problems.append(f"{where} display_order={order!r} 不是整数")

        parent = r.get("parent_key") or ""
        if parent and parent not in keys:
            problems.append(f"{where} 的父字段 {parent!r} 在本表里不存在")

        # ---- 例句出处（签字文档 §6）----
        # 数据库的 CHECK 也能拦，但它只会说 constraint failed。
        # 对着 89 行的 Excel 表，会计同学需要知道的是「第几行、哪一格填错了」。
        page = str(r.get("example_page") or "").strip()
        file_ = str(r.get("example_file") or "").strip()
        source = str(r.get("example_source") or "").strip()

        if page and (_to_int(page) is None or _to_int(page) <= 0):
            problems.append(f"{where} example_page={page!r} 不是正整数页码")

        if source == "annual_report":
            if not file_:
                problems.append(
                    f"{where} 标为年报原文，example_file（PDF 文件名）不能为空"
                )
            if not page:
                problems.append(f"{where} 标为年报原文，example_page（页码）不能为空")
        elif source == "synthetic_example" and (file_ or page):
            problems.append(
                f"{where} 是占位符句，却填了 example_file/example_page —— "
                "要么把例句换成年报原文并改成 annual_report，要么清空这两格"
            )

    return problems


def _to_db_values(rows: list[dict]) -> list[dict]:
    """把 CSV 单元格转成入库形态：多值列回 JSON 数组，布尔列回 0/1。"""
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        for col in dictionary.JSON_COLUMNS:
            d[col] = dictionary.as_json_list(dictionary._parse_multi(r.get(col)))
        for col in ("is_nonrecurring", "is_derived"):
            d[col] = _to_flag(r.get(col))
        d["display_order"] = _to_int(r.get("display_order"))
        d["industry"] = r.get("industry") or None
        d["parent_key"] = r.get("parent_key") or None
        d["example_sentence"] = r.get("example_sentence") or None
        d["example_source"] = r.get("example_source") or None
        d["example_file"] = r.get("example_file") or None
        d["example_page"] = _to_int(r.get("example_page"))
        d["scope_note"] = r.get("scope_note") or None
        d["note"] = r.get("note") or None
        out.append(d)
    return out


def _canonical(row: dict) -> dict:
    """把一行（无论来自 SQL 还是 CSV）归一化成可直接比较的形态。

    SQL 侧的多值列是 Python 列表、布尔是 0/1 整数；CSV 侧全是字符串。
    不归一化就逐列比，会满屏假差异，真正的不一致反而被淹没。
    """
    out: dict[str, object] = {}
    for col in dictionary.COLUMNS:
        value = row.get(col)
        if col in dictionary.JSON_COLUMNS:
            out[col] = dictionary.as_json_list(dictionary._parse_multi(value))
        elif col in ("is_nonrecurring", "is_derived"):
            out[col] = _to_flag(value)
        elif col in ("display_order", "example_page"):
            # example_page 也必须归一化：SQL 侧读回来是 int，CSV 侧是 '86'，
            # 不归一化会让 --check 每次都对这一列报假差异。
            out[col] = _to_int(value)
        else:
            out[col] = str(value).strip() if value is not None else ""
    return out


def _to_flag(value: object) -> int:
    text = str(value or "").strip().lower()
    return 0 if text in ("", "0", "false", "no", "否", "n") else 1


def _to_int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _diff_against_seed(rows: list[dict]) -> list[str]:
    """列出相对当前种子文件的增删，供人确认。"""
    con = _memory_with_seed()
    existing = {r["metric_key"] for r in dictionary.fetch(con)}
    con.close()
    incoming = {r["metric_key"] for r in rows}
    notes = []
    added = sorted(incoming - existing)
    removed = sorted(existing - incoming)
    if added:
        notes.append(f"新增 {len(added)} 个字段：{'、'.join(added)}")
    if removed:
        notes.append(f"删除 {len(removed)} 个字段：{'、'.join(removed)}")
    return notes


def cmd_import(csv_path: Path, allow_delete: bool) -> int:
    rows, problems = _read_csv(csv_path)

    # ---- 装库之前先按行查一遍，报错带上 Excel 的行号 ----
    seen: dict[str, int] = {}
    for i, r in enumerate(rows, start=2):  # 第 1 行是表头
        key = r["metric_key"]
        if key in seen:
            problems.append(f"第 {i} 行 metric_key {key!r} 与第 {seen[key]} 行重复")
        seen[key] = i
    problems.extend(_preflight(rows))
    if problems:
        return _report(problems, written=False)

    db_rows = _to_db_values(rows)
    deletions = _diff_against_seed(db_rows)
    if any("删除" in n for n in deletions) and not allow_delete:
        return _report(
            deletions
            + [
                "CSV 里少了上面这些字段。删除字典条目会让已有事实失去指标定义，"
                "所以默认拒绝——确认要删就加 --allow-delete。"
            ],
            written=False,
        )

    # ---- 先试装：把候选内容灌进内存库，跑完整校验 ----
    # 这样 SQL 语法错误、CHECK 约束冲突、字典规则违例都在写盘之前暴露
    try:
        candidate = _compose_seed_text(db_rows)
        con = _memory_with_seed(candidate)
    except sqlite3.Error as e:
        return _report([f"生成的 SQL 无法执行：{e}"], written=False)

    problems = dictionary.validate(con)
    con.close()
    if problems:
        return _report(problems, written=False)

    SEED_PATH.write_text(candidate, encoding="utf-8")
    for note in deletions:
        print(f"  · {note}")
    print(f"✓ 已写入 {len(db_rows)} 个字段 → {SEED_PATH}")
    print("  提示：python scripts/init_db.py --force 重建数据库，再跑 python -m pytest")
    return 0


def _compose_seed_text(db_rows: list[dict]) -> str:
    """把候选数据嵌回种子文件的生成块，返回完整文件内容。"""
    current = _read_seed_text()
    body = dictionary.render_sql(db_rows)
    return dictionary.replace_generated_block(current, body)


def _report(problems: list[str], written: bool) -> int:
    head = "✗ 未写入，原文件未改动。发现以下问题：" if not written else "✗ 已写入但有问题："
    print(head, file=sys.stderr)
    for p in problems:
        print(f"  · {p}", file=sys.stderr)
    return 1


def cmd_check(csv_path: Path) -> int:
    """校验 CSV 与种子 SQL 是否同步，并跑一遍字典规则。"""
    problems: list[str] = []

    rows, read_problems = _read_csv(csv_path)
    problems.extend(read_problems)

    con = _memory_with_seed()
    try:
        if not problems:
            problems.extend(dictionary.validate(con))
            sql_rows = {r["metric_key"]: r for r in dictionary.fetch(con)}
            in_csv = {r["metric_key"] for r in rows}

            only_sql = sorted(set(sql_rows) - in_csv)
            only_csv = sorted(in_csv - set(sql_rows))
            if only_sql:
                problems.append(f"只在 SQL 里、不在 CSV 里：{'、'.join(only_sql)}")
            if only_csv:
                problems.append(f"只在 CSV 里、不在 SQL 里：{'、'.join(only_csv)}")

            # 键一致时逐列比对。两边都先归一化——SQL 侧是 Python 列表、
            # CSV 侧是分隔符字符串，直接比会满屏假差异，真正的不一致反而被淹没。
            for r in rows:
                ref = sql_rows.get(r["metric_key"])
                if ref is None:
                    continue
                left, right = _canonical(ref), _canonical(r)
                for col in dictionary.COLUMNS:
                    if left[col] != right[col]:
                        problems.append(
                            f"[{r['metric_key']}] {col} 不一致："
                            f"SQL={left[col]!r} CSV={right[col]!r}"
                        )
    finally:
        con.close()

    if problems:
        return _report(problems, written=False)
    print(f"✓ CSV 与种子数据一致，字典规则校验通过（{len(rows)} 个字段）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="字段字典 CSV 往返工具")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export", action="store_true", help="SQL → CSV")
    group.add_argument("--import", dest="do_import", action="store_true", help="CSV → SQL")
    group.add_argument("--check", action="store_true", help="校验两者是否同步")
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help=f"CSV 路径（默认 {CSV_PATH.name}）")
    parser.add_argument(
        "--allow-delete",
        action="store_true",
        help="允许 CSV 里缺少的字段被删除（默认拒绝，防止误删整行）",
    )
    args = parser.parse_args()

    if args.export:
        return cmd_export(args.csv)
    if args.do_import:
        return cmd_import(args.csv, args.allow_delete)
    return cmd_check(args.csv)


if __name__ == "__main__":
    raise SystemExit(main())
