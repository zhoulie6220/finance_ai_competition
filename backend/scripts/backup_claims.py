"""备份 / 恢复**程序生成**的主张数据。

用法：
    python scripts/backup_claims.py --save          # 存到 var/claims_backup.json
    python scripts/backup_claims.py --restore       # 从备份恢复

## 为什么需要它

`init_db.py --force` 会把整库清空，而 `merge_data_pack.py` 只从**数据包**
恢复——数据包里没有主张。于是重建一次，LLM 抽出来的上千条主张就没了。

而它们**是要花钱才能再生成的**（实测一次全量约 5 元、半小时）。
这和数据包那 743 条事实是同一类问题：**能生成不等于愿意再生成一遍**。

## 备份什么

只备份**生成物**，不备份能从种子重建的东西（字段字典、规则参数）：

    claim / claim_indicator / claim_match   LLM 与规则法抽出的主张与判定
    llm_call                                模型调用记录（证据链，且防重复付费）
    q2_aging / risk_disclosure_check / p_confirmation
                                            会计人工录入的内容

最后三张表是**人工抄进来的**——丢了要请会计重抄一遍，比花钱更麻烦。

## 怎么恢复

恢复用 `INSERT OR IGNORE`，所以：
· 已经存在的行不会被覆盖
· 重复恢复是幂等的
· 外键顺序按依赖排好（claim 在 claim_indicator / claim_match 之前）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# _console 与本文件同目录。**这一行不能省**：
# 直接 `python scripts/x.py` 时 Python 会自动把脚本目录放进 sys.path，
# 但测试用 `spec_from_file_location` 按路径加载脚本时**不会**——
# 少了它，`import _console` 只在跑测试时炸，看起来像测试坏了。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402  (与本文件同目录)

_console.setup()

from app.db.session import connect  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"
BACKUP_PATH = BACKEND_DIR / "var" / "claims_backup.json"

#: 按外键依赖顺序排列。**顺序不能换**——
#: claim_indicator 与 claim_match 都外键指向 claim，
#: 反过来插会违反外键（而拒绝发生在整批写入的中途）。
TABLES: tuple[str, ...] = (
    "claim",
    "claim_indicator",
    "claim_match",
    "llm_call",
    "q2_aging",
    "risk_disclosure_check",
    "p_confirmation",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="备份/恢复程序生成的主张数据")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--save", action="store_true")
    group.add_argument("--restore", action="store_true")
    group.add_argument("--stats", action="store_true", help="只看两边各有多少行")
    parser.add_argument("--file", default=None, help=f"备份文件，默认 {BACKUP_PATH}")
    args = parser.parse_args()

    path = Path(args.file) if args.file else BACKUP_PATH
    if not DB_PATH.exists():
        print(f"✗ 数据库不存在：{DB_PATH}", file=sys.stderr)
        return 1

    con = connect(DB_PATH)
    try:
        if args.stats:
            _stats(con, path)
            return 0
        if args.save:
            return _save(con, path)
        return _restore(con, path)
    finally:
        con.close()


def _save(con: sqlite3.Connection, path: Path) -> int:
    payload: dict[str, list[dict]] = {}
    for table in TABLES:
        try:
            rows = con.execute(f'SELECT * FROM "{table}"').fetchall()
        except sqlite3.OperationalError:
            payload[table] = []
            continue
        payload[table] = [dict(r) for r in rows]

    path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    total = sum(len(v) for v in payload.values())
    print(f"✓ 已备份 {total} 行 → {path}")
    for table in TABLES:
        print(f"    {table:<24} {len(payload[table]):>5}")
    return 0


def _restore(con: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        print(f"✗ 找不到备份文件：{path}", file=sys.stderr)
        return 1

    payload = json.loads(path.read_text(encoding="utf-8"))
    restored = 0
    with con:
        for table in TABLES:            # 按依赖顺序
            rows = payload.get(table) or []
            if not rows:
                continue
            cols = list(rows[0])
            collist = ", ".join(f'"{c}"' for c in cols)
            placeholders = ", ".join("?" * len(cols))
            before = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            con.executemany(
                f'INSERT OR IGNORE INTO "{table}" ({collist}) VALUES ({placeholders})',
                [tuple(r[c] for c in cols) for r in rows],
            )
            after = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            added = after - before
            restored += added
            print(f"    {table:<24} 备份 {len(rows):>5}  新增 {added:>5}  现有 {after:>5}")

    print()
    print(f"✓ 已恢复 {restored} 行（INSERT OR IGNORE，已有的不会覆盖）")
    print("  下一步：python scripts/verify_db.py 与 python -m pytest")
    return 0


def _stats(con: sqlite3.Connection, path: Path) -> None:
    print(f"备份文件：{path}  {'存在' if path.exists() else '**不存在**'}")
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    print()
    print(f"  {'表':<24} {'库里':>7} {'备份':>7}")
    for table in TABLES:
        try:
            n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except sqlite3.OperationalError:
            n = -1
        print(f"  {table:<24} {n:>7} {len(payload.get(table) or []):>7}")


if __name__ == "__main__":
    raise SystemExit(main())
