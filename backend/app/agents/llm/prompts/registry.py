"""Prompt 版本化注册表。

赛事要求提交「Prompt」这一类模块，且要说明版本与调用方式。评审会问的一个很具体
的问题是：**同一次运行到底用了哪一版提示词？** 如果答案只是「代码里那段字符串」，
那么改一个标点就会让历史结果无法复现，而且没人知道改过。

所以每个 prompt 一个文件，文件名带版本号，注册表按内容算 sha256：

    claim_extract.v1.md
    claim_extract.v2.md      ← 改过就新增一个版本，**不覆盖旧的**
    memo_text.v1.md

版本号进 `llm_call.prompt_version`，内容哈希进 `llm_call.prompt_hash`，
整表哈希进 `run_manifest.prompt_registry_hash`。三者都留，是因为：
版本号是给人读的、内容哈希是给机器比对的、整表哈希是用来判断「这次运行与上次
是不是同一套提示词」。

**旧版本文件不许删。** 删了之后，历史 llm_call 记录里的 prompt_hash 就再也
找不到对应文件，证据链断在那里，而且不会有任何报错。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# 占位符语法：{变量名}。刻意不用 str.format 的完整语法（它支持 {0} {a.b} 之类），
# 变量名收敛成标识符，模板里出现别的花括号一律当成写错。
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 文件名形如 {key}.{version}.md；key 允许点号分段（claim.extract.v1.md 也认）。
FILE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.]*)\.(?P<version>v\d+)\.md$")


class PromptError(Exception):
    """prompt 缺失、格式不对或变量没填全。

    刻意用异常而不是返回空字符串：**没填的占位符替换成空串是最危险的**——
    模型收到一个语义残缺的提示词，照样会输出一段看起来合理的话，
    而没有任何地方会报错。
    """


@dataclass(frozen=True)
class Prompt:
    key: str
    version: str
    text: str
    sha256: str
    path: Path

    def render(self, variables: dict[str, str]) -> str:
        """填变量。缺任何一个都抛错，不做静默空串替换。"""
        needed = set(PLACEHOLDER_RE.findall(self.text))
        missing = sorted(needed - set(variables))
        if missing:
            raise PromptError(
                f"prompt {self.key}@{self.version} 缺少变量：{'、'.join(missing)}。"
                f"缺少的占位符会被原样留下或被替换成空串，模型照样输出一段"
                f"看起来合理的话，不会报错——所以这里直接拒绝。"
            )
        # 只替换模板里真正出现的键，多余的键忽略（调用方常带共享上下文）
        return PLACEHOLDER_RE.sub(
            lambda m: str(variables[m.group(1)]), self.text
        )

    def placeholders(self) -> tuple[str, ...]:
        return tuple(sorted(set(PLACEHOLDER_RE.findall(self.text))))


class PromptRegistry:
    """从目录加载全部 prompt 文件。目录不存在时为空注册表，不抛错。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._by_key: dict[str, dict[str, Prompt]] = {}
        self._load()

    def _load(self) -> None:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*.md")):
            m = FILE_RE.match(path.name)
            if not m:
                # CHANGELOG.md 之类不属于 prompt，静静跳过；但名字像 prompt 却
                # 不符合命名规则的要在别处报（见 _looks_like_prompt）。
                continue
            raw = path.read_bytes()
            prompt = Prompt(
                key=m.group("key"),
                version=m.group("version"),
                text=raw.decode("utf-8"),
                sha256=hashlib.sha256(raw).hexdigest(),
                path=path,
            )
            self._by_key.setdefault(prompt.key, {})[prompt.version] = prompt

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_key))

    def versions(self, key: str) -> tuple[str, ...]:
        return tuple(sorted(self._by_key.get(key, {})))

    def get(self, key: str, version: str | None = None) -> Prompt:
        """取 prompt。不给版本号则取该键下**版本号最大**的那一个。"""
        versions = self._by_key.get(key)
        if not versions:
            raise PromptError(
                f"没有名为 {key!r} 的 prompt。已有的：{'、'.join(self.keys()) or '（空）'}"
            )
        if version is None:
            version = max(versions, key=_version_number)
        if version not in versions:
            raise PromptError(
                f"prompt {key!r} 没有版本 {version!r}。已有：{'、'.join(sorted(versions))}"
            )
        return versions[version]

    def registry_hash(self) -> str:
        """整表哈希：把所有 prompt 的 (key, version, sha256) 排序后一起哈希。

        进 run_manifest.prompt_registry_hash。**只要任何一个 prompt 改了一个字，
        这个值就变**——它是「这次运行与上次是不是同一套提示词」的判据。
        """
        digest = hashlib.sha256()
        for key in self.keys():
            for version in sorted(self._by_key[key]):
                p = self._by_key[key][version]
                digest.update(f"{key}\x00{version}\x00{p.sha256}\n".encode())
        return digest.hexdigest()


def _version_number(version: str) -> int:
    try:
        return int(version.lstrip("v"))
    except ValueError:  # pragma: no cover - 受 FILE_RE 约束，正常到不了
        return 0
