"""提示词里的枚举必须与数据库约束**同源**。

## 这个测试防的是什么

`claim.claim_type` 的 CHECK 约束只接受八个取值，而提示词也要给模型一份
取值表。**两边各写一份的话，改了一边另一边还是旧的**——而错误要到
写库那一刻才暴露，那时已经跑了几十次模型调用了：

    sqlite3.IntegrityError: CHECK constraint failed: claim_type

更糟的是**拒绝发生在整批写入的中途**：前面写进去的回滚、后面的全没写。

实测踩过：v1 的提示词让模型返回 `cost`，而 CHECK 里没有 `cost`
（数据库用的是 `other` 和 `macro`）。光看提示词完全正常，
跑到写库才炸。

## 所以

从 `schema.sql` 解析出约束，从**最新版**提示词解析出取值表，断言两者一致。
改了任何一边而忘了另一边，这个测试立刻红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agents.llm.prompts.registry import PromptRegistry

BACKEND_DIR = Path(__file__).resolve().parents[3]
SCHEMA = BACKEND_DIR / "app" / "db" / "schema.sql"
PROMPTS_DIR = BACKEND_DIR / "app" / "agents" / "llm" / "prompts"


def _db_claim_types() -> set[str]:
    sql = SCHEMA.read_text(encoding="utf-8")
    m = re.search(
        r"claim_type\s+TEXT NOT NULL\s+CHECK \(claim_type IN \((.*?)\)\)", sql, re.S
    )
    assert m, "schema.sql 里没找到 claim_type 的 CHECK 约束——它被改过？"
    return set(re.findall(r"'(\w+)'", m.group(1)))


def _prompt_claim_types(text: str) -> set[str]:
    """从提示词的 JSON 示例里抠出 claim_type 的取值表。

    找 `"claim_type": "a | b | c"` 那一行。
    """
    m = re.search(r'"claim_type":\s*"([^"]+)"', text)
    assert m, "提示词里没找到 claim_type 的取值表"
    return {x.strip() for x in m.group(1).split("|") if x.strip()}


def test_latest_prompt_matches_db_constraint() -> None:
    """★ 最新版提示词的 claim_type 取值，必须与数据库 CHECK 逐字一致。"""
    db_types = _db_claim_types()
    prompt = PromptRegistry(PROMPTS_DIR).get("claim_extract")
    prompt_types = _prompt_claim_types(prompt.text)

    assert prompt_types == db_types, (
        f"提示词 {prompt.key}@{prompt.version} 的 claim_type 取值与数据库不一致：\n"
        f"  数据库有、提示词没有：{sorted(db_types - prompt_types)}\n"
        f"  提示词有、数据库没有：{sorted(prompt_types - db_types)}\n"
        f"  「提示词有、数据库没有」的那几个会让 INSERT 被拒绝，"
        f"而且是在整批写入的中途才暴露。"
    )


def test_every_historical_version_is_kept() -> None:
    """★ 旧版本文件**不许删**。

    删了之后历史 `llm_call` 记录里的 `prompt_hash` 就找不到对应文件，
    证据链断在那里，而且不会有任何报错。
    """
    versions = PromptRegistry(PROMPTS_DIR).versions("claim_extract")
    assert "v1" in versions, "v1 被删了——历史调用记录会断链"
    assert len(versions) >= 2


def test_registry_picks_the_highest_version() -> None:
    """不给版本号时取最高版本——调用方不该关心当前是第几版。"""
    reg = PromptRegistry(PROMPTS_DIR)
    assert reg.get("claim_extract").version == max(
        reg.versions("claim_extract"), key=lambda v: int(v.lstrip("v"))
    )


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_versions_can_still_be_pinned(version: str) -> None:
    """但必须还能**指定版本**——复现历史结果时要用上。"""
    prompt = PromptRegistry(PROMPTS_DIR).get("claim_extract", version)
    assert prompt.version == version
    assert prompt.sha256


def test_registry_hash_changes_with_any_prompt_change(tmp_path: Path) -> None:
    """整表哈希是「这次运行与上次是不是同一套提示词」的判据。

    新增一个版本也要让它变——否则「换了提示词」在 run_manifest 里看不出来。
    """
    src_dir = PROMPTS_DIR
    before = PromptRegistry(src_dir).registry_hash()

    (tmp_path / "claim_extract.v1.md").write_text("x", encoding="utf-8")
    after = PromptRegistry(tmp_path).registry_hash()
    assert before != after
