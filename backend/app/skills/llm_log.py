"""模型调用的落库出口。

`app/agents/llm/client.py` 每发一次请求就产出一条 `CallRecord`，
这一层把它写进 `llm_call` 表。

**失败路径也要落。** 证据链恰好在最需要它的时候（出错那次）断掉，
是最糟糕的失败方式——事后想查「当时到底发了什么、模型回了什么」，
偏偏那一条没记。

## 密钥

`CallRecord.params` 里**不含密钥**（`LlmSettings.public_params()` 刻意只暴露
model / temperature / seed / offline_mode）。落库前还会再断言一次——
两边都挡，是因为「密钥泄漏」这件事的代价和「忘记挡一次」的成本完全不对等。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator

from app.agents.llm.client import CallRecord


def make_sink(con: sqlite3.Connection) -> "CallSinkImpl":
    """返回一个可以喂给 `LlmClient(sink=...)` 的落库出口。"""
    return CallSinkImpl(con)


@dataclass
class CallSinkImpl:
    """把 `CallRecord` 写进 `llm_call`。

    刻意做成可调用的对象（而不是一个闭包），这样调用方还能拿到
    `written` 列表做统计——比如「这次跑了多少次真实调用」。
    """

    con: sqlite3.Connection
    written: list[str] | None = None

    def __post_init__(self) -> None:
        if self.written is None:
            self.written = []

    def __call__(self, record: CallRecord) -> None:
        blob = json.dumps(record.params, ensure_ascii=False)
        # 双保险：适配层已经保证 params 不含密钥，这里再验一次。
        # 真出了事，宁可在写库前炸掉，也不要让密钥落进数据库。
        if "sk-" in blob:
            raise RuntimeError(
                "拒绝写入：llm_call.params 里出现了疑似密钥。"
                "检查 LlmSettings.public_params() 是不是被人改过。"
            )

        self.con.execute(
            "INSERT OR REPLACE INTO llm_call (call_id, task_id, step_id, purpose,"
            " model, model_version, prompt_key, prompt_version, prompt_hash, params,"
            " input_hash, input_digest, output, tokens_in, tokens_out, latency_ms,"
            " cached, cassette_id, status, error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.call_id,
                record.task_id,
                record.step_id,
                record.purpose,
                record.model,
                record.model_version,
                record.prompt_key,
                record.prompt_version,
                record.prompt_hash,
                blob,
                record.input_hash,
                None,                      # input_digest：prompt 原文另存，这里留空
                record.output,
                record.tokens_in,
                record.tokens_out,
                record.latency_ms,
                1 if record.cached else 0,
                record.cassette_id,
                record.status,
                record.error,
                record.created_at,
            ),
        )
        self.con.commit()
        assert self.written is not None
        self.written.append(record.call_id)


def load_calls(
    con: sqlite3.Connection, *, purpose: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """读回调用记录，供「任务与日志」页展示。"""
    clause = " WHERE purpose = ?" if purpose else ""
    params: list[Any] = [purpose] if purpose else []
    params.append(limit)

    rows = con.execute(
        "SELECT call_id, purpose, model, model_version, prompt_key, prompt_version,"
        " prompt_hash, tokens_in, tokens_out, latency_ms, cached, cassette_id,"
        " status, error, created_at, substr(output, 1, 400) AS output_preview"
        f" FROM llm_call{clause} ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def call_stats(con: sqlite3.Connection) -> dict[str, Any]:
    """调用概况。页面上显示「这次跑了多少次真实调用、花了多少 token」。"""
    row = con.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN cached = 1 THEN 1 ELSE 0 END) AS cached,
               SUM(COALESCE(tokens_in, 0)) AS tokens_in,
               SUM(COALESCE(tokens_out, 0)) AS tokens_out,
               SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)) AS tokens_total
        FROM llm_call
        """
    ).fetchone()
    return {k: (row[k] or 0) for k in row.keys()}


def iter_replayable(con: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    """能用来做离线回放的调用（成功的、带原始输出的）。

    现场演示前用它可以确认「该录的 cassette 都录了」——
    漏录一个场景，断网演示时就会在那里卡住。
    """
    rows = con.execute(
        "SELECT call_id, prompt_key, prompt_version, cassette_id, output"
        " FROM llm_call WHERE status = 'succeeded' AND output IS NOT NULL"
    ).fetchall()
    for r in rows:
        yield dict(r)
