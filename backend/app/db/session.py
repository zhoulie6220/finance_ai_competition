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
#: 默认库位置。实际取值以 `settings.data_root` 为准，见 `default_db_path()`。
DEFAULT_DB_PATH = BACKEND_DIR / "var" / "finance.db"


def default_db_path() -> Path:
    """当前生效的数据库路径。

    放在函数里而不是模块级常量：配置可以在运行期被测试改掉，
    模块级常量一旦求值就固化了，会让「改 DATA_ROOT」变成一句空话。
    """
    from app.config import get_settings

    return get_settings().db_path

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

    不传 `db_path` 时取 `settings.data_root / finance.db`。**这一点很重要**：
    `.env` 里 `DATA_ROOT` 是可配的，如果这里写死 `DEFAULT_DB_PATH`，
    改 `DATA_ROOT` 就只会影响文件读写、不影响数据库——两半数据落在不同地方，
    而两边都不会报错。测试也正是靠它把库指到临时目录。
    """
    path = Path(db_path) if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # ⚠ check_same_thread=False 是必须的，理由不是「为了省事」：
    #   FastAPI 把**同步**路由与同步依赖都丢进线程池执行，而且不保证是同一个线程。
    #   于是「依赖里建的连接，在路由里用」会随机撞上
    #   `SQLite objects created in a thread can only be used in that same thread`。
    #   dev 环境常常侥幸不复现，上线后每个同步接口都可能间歇性 500。
    #
    #   这样安全的前提是**一条连接只归一个请求/一个任务所有**（本项目的做法：
    #   `app/api/deps.py` 每个请求开一条，后台任务各开一条）。
    #   一旦有人把一条连接跨请求共享、或多协程交错使用，这条保护就没了——
    #   那时会出现事务边界错乱，且**不会报错**。改这里之前请先确认这一点。
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
    con = sqlite3.connect(":memory:", check_same_thread=False)
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
