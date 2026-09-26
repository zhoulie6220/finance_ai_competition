"""SQLite 连接管理。

**所有**数据库连接都必须经由 `connect()` 创建，不要直接调用 `sqlite3.connect()`。

原因：`PRAGMA foreign_keys` 是**每连接**生效的，不是数据库级别的设置。在 schema.sql
里写一次 `PRAGMA foreign_keys = ON` 只能作用于执行那条语句的连接；此后应用开的每一条
新连接外键默认都是关闭的，全部外键约束会静默失效——插进一个引用不存在 file_id 的
财务事实也不会报错。这类问题不会以异常的形式暴露，只会让数据慢慢变脏。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = BACKEND_DIR / "var" / "finance.db"

DB_PATH_ENV_HINT = (
    "查看数据：python -m sqlite3 var/finance.db（Python 3.12+ 自带），"
    "或装 VS Code 的 SQLite Viewer 扩展直接点开 .db 文件。"
)


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开一条已正确配置的连接。

    - `foreign_keys=ON`  外键约束生效（每连接必须显式开启）
    - `journal_mode=WAL` 允许多读单写。此设置持久化在库文件里，重复设置无副作用
    - `busy_timeout`     遇到写锁时等待而非立刻报错，避免并发下随机失败
    - `synchronous=NORMAL`  WAL 模式下的推荐值，兼顾安全与速度
    - `row_factory=sqlite3.Row`  便于按列名取值
    - `check_same_thread=False`  允许连接被「换一个线程」使用，见下

    ⚠ **为什么必须关掉线程检查**

    FastAPI 把同步的接口函数丢进**线程池**执行，而同一个请求的依赖注入
    与接口函数**不保证落在同一个线程**。于是会出现：

        依赖里 connect()          → 线程 A
        接口函数用这条连接查询     → 线程 B    ✗ ProgrammingError

    实测表现是**随机 500，刷新一下又好了**——因为下次请求恰好分到同一个线程。
    这种「偶发、重试能过」的错误最难查，也最容易在演示当天出现。

    关掉检查是安全的，前提是**每条连接只属于一个请求、不会有两处同时用它**。
    本项目的所有连接都用 `with con:` 包住写入、请求结束即 close，
    没有任何一处把连接存下来跨请求复用。**不要打破这个前提**——
    真出现两个线程同时用一条连接，SQLite 的行为是未定义的。
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row

    # 顺序有讲究：foreign_keys 必须在任何 DML 之前设置，否则会被忽略
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA busy_timeout = 5000")

    # 自检：外键若没能开启，宁可立刻失败，也不要带着关闭的外键继续跑
    if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        con.close()
        raise RuntimeError(
            "外键约束未能开启。这通常意味着连接上已经有未提交的事务——"
            "请确认没有在 connect() 之前执行过 DML。"
        )
    return con


def connect_memory() -> sqlite3.Connection:
    """内存库，供测试使用。schema 需另行加载。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        con.close()
        raise RuntimeError("外键约束未能开启")
    return con


def init_schema(con: sqlite3.Connection) -> None:
    """在给定连接上加载 schema.sql。测试与初始化脚本共用。"""
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    con.executescript(schema_path.read_text(encoding="utf-8"))


def load_seeds(con: sqlite3.Connection) -> list[str]:
    """按文件名顺序加载种子数据，返回已加载的文件名列表。"""
    seed_dir = BACKEND_DIR / "app" / "data" / "seed"
    loaded: list[str] = []
    for seed in sorted(seed_dir.glob("*.sql")):
        con.executescript(seed.read_text(encoding="utf-8"))
        loaded.append(seed.name)
    return loaded
