"""版本化 Prompt。每个 prompt 一个文件，文件名 {key}.{version}.md。

旧版本文件**不许删**——删了之后历史 llm_call 记录里的 prompt_hash 就找不到
对应文件，证据链断在那里且不报错。改动一律新增版本号，见 CHANGELOG.md。
"""

from app.agents.llm.prompts.registry import (
    Prompt,
    PromptError,
    PromptRegistry,
)

__all__ = ["Prompt", "PromptError", "PromptRegistry"]
