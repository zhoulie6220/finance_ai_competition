"""LLM 接入配置。全部来自环境变量或 backend/.env。

**密钥只从这里读，绝不写进任何日志、数据库或返回值。**
`llm_call.params` 那一列的注释明确写了「不含密钥」——所以下面的
`public_params()` 刻意只暴露与结果复现有关的参数。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

BACKEND_DIR = Path(__file__).resolve().parents[3]
DEFAULT_ENV_PATH = BACKEND_DIR / ".env"
DEFAULT_CASSETTE_DIR = BACKEND_DIR / "app" / "data" / "cassettes"

Mode = Literal["live", "replay"]


def _load_dotenv(path: Path) -> dict[str, str]:
    """读 .env。**不覆盖已存在的环境变量**——线上环境优先。

    用 python-dotenv（锁文件里有）；它不在时安静降级为不加载，
    因为 .env 本来就是可选的：不配也能跑，只是模型相关的功能不可用。
    """
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - 锁文件里有，正常到不了
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _as_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class LlmSettings:
    """一次运行用到的模型接入参数。

    构造后不再变化，且**不含任何行为分支**——模式判断集中在 `mode` 一个属性里，
    免得「离线还是在线」散落在各处，某处漏判就会在断网的现场发起真实请求。
    """

    base_url: str = "https://api.deepseek.com"
    api_key: str | None = None
    model: str = "deepseek-chat"
    temperature: float = 0.0
    seed: int | None = 42
    offline_mode: bool = False
    cassette_dir: Path = DEFAULT_CASSETTE_DIR
    timeout_s: float = 60.0
    # schema 校验失败后额外重试几次。**不能无限重试**，也不能吞掉第二次失败。
    max_repair_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_repair_attempts < 0:
            raise ValueError("max_repair_attempts 不能为负")
        if self.temperature < 0:
            raise ValueError("temperature 不能为负")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s 必须为正")

    @property
    def mode(self) -> Mode:
        """`replay` 时不发起任何真实请求。"""
        return "replay" if self.offline_mode else "live"

    @property
    def configured(self) -> bool:
        """是否配了密钥。没配时只有 replay 能跑。"""
        return bool(self.api_key and self.api_key.strip() and "在此填入" not in self.api_key)

    def public_params(self) -> dict[str, object]:
        """可以落库、可以出现在日志里的参数。**不含密钥**。"""
        return {
            "model": self.model,
            "temperature": self.temperature,
            "seed": self.seed,
            "offline_mode": self.offline_mode,
        }

    def describe(self) -> str:
        """给人看的一句话状态，用于日志与 /api/health。"""
        if self.offline_mode:
            return f"离线回放模式（{self.model}，从 {self.cassette_dir.name}/ 读预录响应）"
        if not self.configured:
            return f"实时模式但未配置密钥（{self.model}）——模型相关功能不可用"
        return f"实时模式（{self.model}）"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        env_path: Path | None = None,
    ) -> "LlmSettings":
        """从环境变量构造。显式传入 `env` 时**完全不读 .env**，便于测试。"""
        if env is None:
            merged: dict[str, str] = dict(_load_dotenv(env_path or DEFAULT_ENV_PATH))
            merged.update(os.environ)          # 真实环境变量优先
            env = merged

        raw_seed = env.get("LLM_SEED")
        cassette = env.get("LLM_CASSETTE_DIR")
        raw_attempts = env.get("LLM_MAX_REPAIR_ATTEMPTS")

        return cls(
            base_url=env.get("LLM_BASE_URL") or "https://api.deepseek.com",
            api_key=env.get("LLM_API_KEY") or None,
            model=env.get("LLM_MODEL") or "deepseek-chat",
            temperature=float(env.get("LLM_TEMPERATURE") or 0.0),
            seed=int(raw_seed) if raw_seed else None,
            offline_mode=_as_bool(env.get("OFFLINE_MODE")),
            cassette_dir=Path(cassette) if cassette else DEFAULT_CASSETTE_DIR,
            timeout_s=float(env.get("LLM_TIMEOUT_S") or 60.0),
            max_repair_attempts=int(raw_attempts) if raw_attempts else 1,
        )
