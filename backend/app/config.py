"""运行时配置。

全部来自环境变量，本地开发时读 `backend/.env`（不入库，见 .gitignore 与
`.env.example`）。**密钥只在这里读一次**，其余模块一律拿 `settings` 对象——
散落的 `os.environ[...]` 是密钥泄漏进日志的常见路径。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 模型接口（DeepSeek，OpenAI 兼容）----
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str | None = None
    llm_model: str = "deepseek-chat"
    # temperature=0 + 固定 seed：同一输入两次运行结果一致，是可复现的前提
    llm_temperature: float = 0.0
    llm_seed: int = 42

    # 先录后放：置 1 时不发真实请求，改读 backend/app/data/cassettes/ 下的预录响应。
    # ⚠ 界面必须显著标注「离线回放模式」，不得假装实时。
    offline_mode: bool = False

    # ---- 数据根目录（白名单）----
    # 后端只允许读写此目录下的文件，所有访问写 file_access_log 表。
    data_root: Path = Field(default=BACKEND_DIR / "var")

    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origin: str = "http://localhost:5173"

    @property
    def cors_origins(self) -> list[str]:
        """允许的前端来源。

        `localhost` 与 `127.0.0.1` 是**两个不同的来源**，浏览器不认为它们等价。
        只放行其中一个的话，换个写法打开页面就会被拦，而报错是一句笼统的
        CORS 提示，跟真正的原因（主机名写法不同）离得很远。
        开发时前端走 Vite 代理、根本不跨域，这里兜的是不带代理直接访问的情况。
        """
        hosts = {self.cors_origin}
        for origin in list(hosts):
            if "//localhost" in origin:
                hosts.add(origin.replace("//localhost", "//127.0.0.1"))
            elif "//127.0.0.1" in origin:
                hosts.add(origin.replace("//127.0.0.1", "//localhost"))
        return sorted(hosts)

    # ---- 日志 ----
    log_level: str = "INFO"

    # ---- SSE ----
    # 每个任务在内存里保留的事件条数上限，供断线重连按 Last-Event-ID 续传。
    # 有界是刻意的：无界历史在长任务上会一直涨，而演示现场不会有人断线几万条再回来。
    sse_history_limit: int = 1000
    # 心跳间隔（秒）。中间有反向代理或浏览器空闲超时时，没有心跳会被掐断。
    sse_heartbeat_seconds: float = 15.0

    @property
    def db_path(self) -> Path:
        return self.data_root / "finance.db"

    def redacted(self) -> dict:
        """给日志/`run_manifest` 用的脱敏副本。"""
        d = self.model_dump()
        d["llm_api_key"] = "已设置" if self.llm_api_key else None
        return d


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
