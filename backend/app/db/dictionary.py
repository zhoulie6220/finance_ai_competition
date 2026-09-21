"""字段字典的校验规则与 SQL 渲染。

字段字典是「PDF 行名 → 字段键」映射的唯一依据，它的错误**不会报错**：
一个别名漏掉，那个字段静默解析不出来；一个排除词写错，两个字段对着同一行争抢，
谁赢取决于遍历顺序。所以规则集中在这里，由三处共用：

  - `tests/unit/db/test_metric_dictionary.py`  回归测试（含反例）
  - `scripts/dict_csv.py`                       会计同学改字典的 CSV 往返
  - `scripts/init_db.py`                        建库时的最终把关

放在一处而不是各写一遍，是因为「校验逻辑分散」本身就是漏检的常见来源：
测试里加了一条规则、导入脚本没加，改字典时照样能把坏数据放进去。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from app.schemas.enums import SignConvention, Statement, UnitKind, ValueType

# CSV 与 SQL 共同的可编辑列，顺序与 INSERT 列一致。
COLUMNS: tuple[str, ...] = (
    "metric_key",
    "label_cn",
    "aliases",
    "exclusion_terms",
    "statement",
    "value_type",
    "unit_kind",
    "sign_convention",
    "is_nonrecurring",
    "is_derived",
    "industry",
    "parent_key",
    "display_order",
    "example_sentence",
    "example_source",
    "scope_note",
    "note",
)

# 多值列在 CSV 单元格里的分隔符。导出用 '|'，导入时也接受 ';'——
# 会计同学上一版 docx 用的是分号，没必要让他们改习惯。
MULTI_SEP = "|"
MULTI_SEP_ALSO = ("|", ";", "；")

JSON_COLUMNS = ("aliases", "exclusion_terms")

# 这两列在 schema 里的可空性不同：aliases 是 NOT NULL，exclusion_terms 可空。
# 渲染时得区别对待——空列表一律写 NULL 会让 aliases 撞上 NOT NULL 约束，
# 一律写 '[]' 又会让「没有排除词」和「排除词是空数组」在文件里长得一样，
# 每次 import 都产生无意义的 diff。
NOT_NULL_JSON_COLUMNS = ("aliases",)

EXAMPLE_SOURCES = ("annual_report", "synthetic_example")

SEED_PATH_PARTS = ("app", "data", "seed", "0001_metric_definitions.sql")

BEGIN_MARK = "-- BEGIN GENERATED: metric_definitions"
END_MARK = "-- END GENERATED: metric_definitions"

# SQL 渲染时的分组标题，与 gen_data_contract_doc.py 的分组保持一致
_SECTION_BY_STATEMENT = {
    "income": "利润表",
    "balance": "资产负债表",
    "cashflow": "现金流量表",
    "indicator": "股本与每股",
    "disclosure": "披露事项（无数值）",
}
_SECTION_BY_INDUSTRY = {"steel": "钢铁专属", "energy": "能源专属"}


# --------------------------------------------------------------------- 读取


def fetch(con: sqlite3.Connection) -> list[dict]:
    """读全部字段定义，多值列还原成 list。"""
    rows = con.execute(
        f"SELECT {', '.join(COLUMNS)} FROM metric_definition ORDER BY display_order, metric_key"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        for col in JSON_COLUMNS:
            d[col] = _parse_multi(d.get(col))
        out.append(d)
    return out


def _parse_multi(raw: object) -> list[str]:
    """把别名/排除词解析成列表。

    三种形态都收，因为这三个地方都有人手写：
      - Python 列表（`fetch()` 已经把 JSON 列还原过一道）
      - JSON 数组字符串（数据库里存的样子）
      - 分隔符拼接的字符串（CSV 里的样子）
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]  # 交给 validate 报错，别在这里吞掉
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed]
        return [text]
    for sep in MULTI_SEP_ALSO:
        if sep in text:
            return [p.strip() for p in text.split(sep) if p.strip()]
    return [text]


def as_json_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


# --------------------------------------------------------------------- 校验


def validate(con: sqlite3.Connection) -> list[str]:
    """校验字典。返回人类可读的问题列表，空列表表示通过。

    每条消息都带上出问题的 metric_key，因为改字典的人未必看得懂 SQL——
    「第 47 行 revenue 的排除词挡住了自己的别名」比「约束失败」有用得多。
    """
    problems: list[str] = []
    rows = con.execute(
        f"SELECT {', '.join(COLUMNS)} FROM metric_definition ORDER BY display_order, metric_key"
    ).fetchall()
    if not rows:
        return ["字段字典为空——种子文件没有加载成功？"]

    keys = {r["metric_key"] for r in rows}

    for r in rows:
        key = r["metric_key"]
        where = f"[{key}]"

        if not (key or "").strip():
            problems.append(f"{where} metric_key 为空")
            continue
        if not (r["label_cn"] or "").strip():
            problems.append(f"{where} 标准名称为空")

        # ---- 枚举取值 ----
        for col, allowed in (
            ("statement", {s.value for s in Statement}),
            ("value_type", {v.value for v in ValueType}),
            ("unit_kind", {u.value for u in UnitKind}),
            ("sign_convention", {s.value for s in SignConvention}),
        ):
            if r[col] not in allowed:
                problems.append(
                    f"{where} {col}={r[col]!r} 不在允许取值内：{'/'.join(sorted(allowed))}"
                )

        # ---- 多值列的 JSON 合法性 ----
        for col in JSON_COLUMNS:
            raw = r[col]
            parsed = _parse_multi(raw)
            if raw is not None and str(raw).strip() and str(raw).strip().startswith("["):
                try:
                    loaded = json.loads(str(raw))
                    if not isinstance(loaded, list):
                        problems.append(f"{where} {col} 不是 JSON 数组")
                except json.JSONDecodeError as e:
                    problems.append(f"{where} {col} 不是合法 JSON：{e}")
            if any(not x.strip() for x in parsed):
                problems.append(f"{where} {col} 含空白项")

        aliases = _parse_multi(r["aliases"])
        exclusions = _parse_multi(r["exclusion_terms"])

        # ---- 排除词不得挡住自己的别名 ----
        # 匹配语义是精确相等，所以重合就意味着这个字段永远映射不上。
        clash = sorted(set(aliases) & set(exclusions))
        if clash:
            problems.append(f"{where} 排除词挡住了自己的别名：{'、'.join(clash)}")

        # ---- 文本型与数值型必须自洽 ----
        is_text = r["value_type"] == "text"
        if is_text and r["unit_kind"] != "text":
            problems.append(f"{where} 是文本型却声明了数值单位 {r['unit_kind']!r}")
        if not is_text and r["unit_kind"] == "text":
            problems.append(f"{where} 是数值型却声明 unit_kind='text'")

        # ---- 披露事项只能是文本 ----
        if r["statement"] == "disclosure" and not is_text:
            problems.append(f"{where} 属于披露事项，必须 value_type='text'")

        # ---- 父字段不得指向自己 ----
        # 「父字段不存在」不在这里查：parent_key 上有外键，等这里跑到的时候
        # 已经不可能不成立了。那种错误由 dict_csv 在装库**之前**按行报，
        # 才能带上 Excel 的行号——外键报错只会甩一句 FOREIGN KEY constraint failed。
        if r["parent_key"] == key:
            problems.append(f"{where} 的父字段指向自己")

        # ---- 例句与来源标记必须成对 ----
        sentence = (r["example_sentence"] or "").strip()
        source = (r["example_source"] or "").strip() or None
        if sentence and source is None:
            problems.append(f"{where} 有例句但没有标注 example_source")
        if not sentence and source is not None:
            problems.append(f"{where} 没有例句却标了 example_source")
        if source is not None and source not in EXAMPLE_SOURCES:
            problems.append(
                f"{where} example_source={source!r} 不在允许取值内："
                f"{'/'.join(EXAMPLE_SOURCES)}"
            )
        # 标成「年报原文」就不能还留着占位符——那等于把合成句当成了真证据
        if source == "annual_report" and "【" in sentence:
            problems.append(
                f"{where} 标为年报原文，但例句里仍有【】占位符——"
                "请替换为真实原句，或改回 synthetic_example"
            )

    # ---- 一个别名只能属于一个字段（同行业内） ----
    owner: dict[str, list[str]] = defaultdict(list)
    industry_of = {r["metric_key"]: r["industry"] for r in rows}
    for r in rows:
        for a in _parse_multi(r["aliases"]):
            owner[a].append(r["metric_key"])
    for alias, owners in sorted(owner.items()):
        if len(owners) < 2:
            continue
        # 跨行业重名是允许的（如「销售量」钢企煤企都有），由 project.industry 过滤
        if len({industry_of[k] for k in owners}) == 1:
            problems.append(
                f"别名 {alias!r} 被多个字段共用：{'、'.join(owners)}"
                "（映射结果会取决于遍历顺序）"
            )

    return problems


# --------------------------------------------------------------------- 渲染


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if not text.strip():
        return "NULL"
    return "'" + text.replace("'", "''") + "'"


def _section_of(row: dict) -> str:
    industry = (row.get("industry") or "").strip()
    if industry in _SECTION_BY_INDUSTRY:
        return _SECTION_BY_INDUSTRY[industry]
    return _SECTION_BY_STATEMENT.get(row.get("statement") or "", "其他")


def render_sql(rows: list[dict]) -> str:
    """把字段定义渲染成 INSERT 语句（不含起止标记）。

    多值列统一写成 JSON 数组——**不要**在 Python 里靠相邻字符串字面量拼接，
    SQL 不做这件事（Python 会），跨行的两个 'abc' 'def' 是语法错误。
    """
    values: list[str] = []
    current_section: str | None = None
    for row in rows:
        section = _section_of(row)
        if section != current_section:
            values.append(f"  -- ---- {section}")
            current_section = section
        cells = []
        for col in COLUMNS:
            value = row.get(col)
            if col in JSON_COLUMNS:
                # 空列表：NOT NULL 列写 '[]'，可空列写 NULL。
                # 目的是让重新渲染前后的文件逐字节一致——否则每次 import
                # 都会产生一整份无意义的 diff，真正的改动反而看不见了。
                parsed = _parse_multi(value)
                if not parsed and col not in NOT_NULL_JSON_COLUMNS:
                    value = None
                else:
                    value = as_json_list(parsed)
            cells.append(_sql_literal(value))
        values.append("  (" + ", ".join(cells) + "),")
    # 末尾的逗号换成 SQL 语句结束符
    if values:
        values[-1] = values[-1].rstrip(",") + ";"

    return (
        "INSERT INTO metric_definition\n"
        f"  ({', '.join(COLUMNS)})\n"
        "VALUES\n" + "\n".join(values)
    )


def replace_generated_block(text: str, body: str) -> str:
    """把 body 替换进 text 的两个标记之间。找不到标记就抛错，不静默追加。"""
    if BEGIN_MARK not in text or END_MARK not in text:
        raise ValueError(f"种子文件里找不到标记 {BEGIN_MARK} / {END_MARK}")
    head, rest = text.split(BEGIN_MARK, 1)
    _, tail = rest.split(END_MARK, 1)
    return f"{head}{BEGIN_MARK}\n{body}\n{END_MARK}{tail}"
