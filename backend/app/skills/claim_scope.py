"""「哪些主张进下游」的**唯一开关**。

## 为什么要有这么一个东西

规则法与模型法抽出来的主张**并存**在 `claim` 表里，靠 `extractor` 区分
（`rule:claim_v1` / `llm:deepseek-chat@<prompt_hash>`）。两边抽到同一句话会存成
两行——`claim_id` 前缀不同（`cl-` 与 `cl-llm-`），是刻意的，否则后写的会覆盖
先写的，「两种抽取法并排对照」这条验收标准就没法做。

问题是**下游有三处各自 `SELECT ... FROM claim`**：

    1. `matching.match_and_store`     落 claim_match（判定表）
    2. `matching.project_index_input` 算 H 与 C（指数）
    3. `scripts/export_input_templates.py::_export_p`  导 P 表给会计

三处各写各的过滤条件，就会**同一条指数里分子来自 A、分母来自 B**——
而页面上两个数字都算得出来，看不出它们不是一套。所以过滤条件只写在这里。

## 两件事

**一、按来源过滤。** 见下面的 `CLAIM_SOURCE`。

**二、同一句话只留一条。** 跨抽取器去重。不做的话，一句话被两台机器都抽到，
它会进 P 表两次（会计要判两遍，可能判出两个结论）、进 H/C 的分母两次
（同一条证据算两票）。实测重建库前的 307 条可验证主张里只有 279 句不同的话，
其中一条被模型自己抽了 **4 遍**。

保留哪一条的判据是**优先规则法**：它的 `claim_id` 是确定性的
（sha1(项目|章节|句序|归一化文本)），换台机器重跑还是同一行；
模型法的 id 依赖 `prompt_hash`，改一个提示词就全变。
「同一句话留一条」时，留那条谁都复现得出来的。

## ⚠ 这个开关现在还没进 rule_config

因为它**不是会计口径参数**，是个工程接线选择——`rule_config` 的 `tier` 只有
`hard`（会计口径须签字）/ `soft` / `model` 三档，塞哪一档都会误导：
标 `hard` 会让会计以为这是他要背的口径，标 `soft` 又等于说它「只是提示语」。

但它**确实需要会计口径裁一次**：v1.1 §A.4 定义 N 时说「已到验证期、对象和目标
可识别的主张数」，**没有说这些主张该由谁来抽**。在那之前，
默认只有规则法——理由是它不需要密钥、不联网、换台机器结果一样。
要改来源，改下面这一个常量。
"""

from __future__ import annotations

import sqlite3
from typing import Any

#: 进下游的候选来源。
#:
#:   'rule'   只用规则法。确定性、可复现、不需要密钥——**当前默认**。
#:   'llm'    只用模型法。
#:   'union'  两者并集（⚠ 去重后仍是两条不同的句子各算一条，
#:            但「哪家公司被抽了两遍」取决于 cassette 覆盖到哪，
#:            三家的分母会来自不同的机器，**不可比**）。
CLAIM_SOURCE = "rule"

#: extractor 列的前缀。落库时按 `f"{前缀}:..."` 写，见 narrative.py 的 EXTRACTOR。
_PREFIX = {"rule": "rule:", "llm": "llm:"}


def scope_sql(
    alias: str = "c", *, include_background: bool = False
) -> tuple[str, tuple[Any, ...]]:
    """返回 (SQL 片段, 参数)，直接 `AND` 进 WHERE 即可。

    `alias` 是外层查询里 `claim` 表的别名——片段里要用它限定列，
    否则和外层同名表撞车，SQLite 会报 ambiguous column，或者更糟：
    悄悄按内层那个来。

    `include_background=True` 只给**统计口径**用（`narrative.claim_stats`）——
    那一处本来就要把背景主张**数出来**报给页面（「不可验证 N 条」），
    排除掉的话那个数永远是 0，而页面上看起来像是「一条背景主张都没有」。
    进**判定与指数**的地方一律用默认值。
    """
    prefix = _PREFIX.get(CLAIM_SOURCE)
    params: tuple[Any, ...] = ()
    source_clause = ""
    if prefix is not None:
        source_clause = f" AND {alias}.extractor LIKE ? || '%'"
        params = (prefix,)

    # ⚠ **标成背景的主张不得进入下游。** 这是 v1.1 与 docs/01 的硬约束
    # （「不可验证的主张必须标 background_only，**不得进入评分**」），
    # 而它原来**只写在 `match_and_store` 里**——判定表守了，指数没守：
    # `_scored_claims` 只看「有没有期间」，于是一批 `background_only=1`
    # **但带着期间**的主张照样进 H/C。实测宝钢 33 条、华菱钢铁 24 条、
    # 首钢 5 条。
    #
    # 后果正是这个模块开头的 docstring 要挡的那一种：**判定表里没有它们、
    # 指数里却算着它们**，两个数字都算得出来，看不出不是一套。
    #
    # 写在这里而不是各调用点：过滤条件只允许有一份。
    background_clause = (
        "" if include_background else f" AND {alias}.background_only = 0"
    )

    # 同一句话只留一条。ROW_NUMBER 需要 SQLite ≥ 3.25（Python 3.12 自带的最新版远高于此）。
    # ⚠ 排序里必须带 claim_id 兜底：同一来源下若真有两行文本相同，
    # 只按 extractor 排序的话谁赢由查询计划决定，**每次跑可能不一样**，
    # 而「哪些主张参与判定」跟着变——那种不稳定查不出来。
    dedup = (
        f" AND {alias}.claim_id IN ("
        "   SELECT claim_id FROM ("
        "     SELECT claim_id,"
        "            ROW_NUMBER() OVER ("
        "              PARTITION BY project_id, claim_text"
        "              ORDER BY CASE WHEN extractor LIKE 'rule:%' THEN 0 ELSE 1 END,"
        "                       claim_id"
        "            ) AS rn"
        "     FROM claim"
        "   ) WHERE rn = 1"
        " )"
    )
    return source_clause + background_clause + dedup, params


def describe() -> str:
    """给脚本打印用的一句话说明。"""
    if CLAIM_SOURCE == "union":
        return "候选来源：规则法 + 模型法（并集，跨抽取器已去重）"
    return f"候选来源：{'规则法' if CLAIM_SOURCE == 'rule' else '模型法'}（跨抽取器已去重）"


def count_in_scope(con: sqlite3.Connection, project_id: str) -> int:
    """当前口径下该项目有多少条可验证主张。给脚本对照用。"""
    clause, params = scope_sql("c")
    return con.execute(
        "SELECT COUNT(*) FROM claim c"
        f" WHERE c.project_id = ? AND c.verifiable = 1{clause}",
        (project_id, *params),
    ).fetchone()[0]
