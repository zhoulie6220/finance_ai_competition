"""`connect()` 的跨线程可用性回归测试。

## 这个 bug 的形态

FastAPI 把同步的接口函数丢进**线程池**执行，而同一个请求的依赖注入与
接口函数**不保证落在同一个线程**。于是：

    依赖里 connect()        → 线程 A
    接口函数用这条连接查询   → 线程 B    ✗ ProgrammingError

实测表现是**随机 500，刷新一下又好了**——下次请求恰好分到同一个线程。

这种「偶发、重试能过」的错误最难查，也最容易在演示当天出现：
彩排时刷新几下就正常了，正式录的时候它偏偏就出来。

`sqlite3.connect(check_same_thread=False)` 修掉了它，但那个开关有前提：
**每条连接只属于一个请求、不会有两处同时用它**。
下面最后一条测试就是在守这个前提。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.db.session import connect, connect_memory, init_schema


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "threads.db"
    con = connect(path)
    try:
        init_schema(con)
        con.execute(
            "INSERT INTO project (project_id, name, company_name, stock_code,"
            " industry, fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
            ("p1", "测试", "某钢铁", "600019.SH", "steel", '["2024"]',
             "2026-09-27T00:00:00"),
        )
        con.commit()
    finally:
        con.close()
    return path


def test_connection_usable_from_another_thread(db_path: Path) -> None:
    """★ 在一个线程建连接、在另一个线程用它——必须能跑。

    这正是 FastAPI 依赖注入 + 同步接口函数的实际行为。
    没有这个保证的话，接口会随机 500，而且刷新一下就好了。
    """
    con = connect(db_path)
    errors: list[BaseException] = []
    rows: list[int] = []

    def query() -> None:
        try:
            rows.append(
                con.execute("SELECT COUNT(*) FROM project").fetchone()[0]
            )
        except BaseException as exc:  # noqa: BLE001 - 要把异常带回主线程
            errors.append(exc)

    thread = threading.Thread(target=query)
    thread.start()
    thread.join()
    con.close()

    assert not errors, f"跨线程使用连接失败了：{errors[0]!r}"
    assert rows == [1]


def test_connection_survives_a_thread_pool(db_path: Path) -> None:
    """模拟线程池：同一条连接先后被多个不同线程使用。

    依赖注入与接口函数可能落在不同的池化线程上，而哪个线程是**不确定的**。
    所以不能只测「换一次能行」，要测「换来换去都行」。
    """
    con = connect(db_path)
    errors: list[BaseException] = []

    def round_trip() -> None:
        try:
            con.execute("SELECT COUNT(*) FROM project").fetchone()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=round_trip) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    con.close()

    assert not errors, f"线程池场景下失败了：{errors[0]!r}"


def test_foreign_keys_still_on_after_the_change(db_path: Path) -> None:
    """关掉线程检查不能顺带把别的配置弄丢。

    外键是**每连接**生效的——`check_same_thread` 改了之后如果忘了重设，
    全部外键约束会静默失效，插进一个引用不存在 file_id 的事实也不报错。
    """
    con = connect(db_path)
    try:
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        con.close()


def test_check_same_thread_is_the_only_thread_relaxation() -> None:
    """★ 守前提：这次改动只放开了线程检查，没有引入共享连接。

    关掉 `check_same_thread` 是安全的，**前提是每条连接只属于一个请求**。
    这条测试盯的是「有人把连接存成模块级单例」这种改法——
    那会让两个线程真的同时用一条连接，而 SQLite 在那种情况下的行为
    是未定义的：轻则报错，重则静默返回错数据。
    """
    import inspect

    from app.db import session

    src = inspect.getsource(session)
    assert "check_same_thread=False" in src

    # 连接不能被缓存在模块级变量里（那样就跨请求共享了）
    module_level = [
        node.targets[0].id
        for node in __import__("ast").parse(src).body
        if isinstance(node, __import__("ast").Assign)
        and isinstance(node.targets[0], __import__("ast").Name)
    ]
    assert not any("conn" in n.lower() or "connection" in n.lower() for n in module_level), (
        "session.py 里出现了模块级的连接变量——那就跨请求共享了，"
        "check_same_thread=False 的前提被打破"
    )


def test_memory_connection_also_relaxed() -> None:
    """内存连接走的是同一个 `sqlite3.connect` 路径吗？

    不是——`connect_memory()` 是另一个函数。它只给测试用、不跨线程，
    所以**不需要**放开；但这条测试把「它确实没被顺手改掉」记下来，
    免得日后有人「统一一下」反而引入了共享。
    """
    con = connect_memory()
    try:
        init_schema(con)
        assert con.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 0
    finally:
        con.close()


def test_cross_thread_write_works(db_path: Path) -> None:
    """不只是读——写也要能跨线程。`/api/checks` 会写 fact_check_result。"""
    con = connect(db_path)
    errors: list[BaseException] = []

    def write() -> None:
        try:
            with con:
                con.execute(
                    "INSERT INTO project (project_id, name, company_name, stock_code,"
                    " industry, fiscal_years, created_at) VALUES (?,?,?,?,?,?,?)",
                    ("p2", "测试2", "某钢铁", "000001.SZ", "steel", '["2024"]',
                     "2026-09-27T00:00:00"),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=write)
    thread.start()
    thread.join()
    count = con.execute("SELECT COUNT(*) FROM project").fetchone()[0]
    con.close()

    assert not errors, f"跨线程写入失败了：{errors[0]!r}"
    assert count == 2


def test_original_behaviour_is_documented() -> None:
    """把这个坑写进文档字符串——下一个人遇到同样的 500 时能搜到。"""
    from app.db.session import connect as connect_fn

    doc = connect_fn.__doc__ or ""
    assert "check_same_thread" in doc
    assert "线程" in doc
    assert "随机 500" in doc or "500" in doc
