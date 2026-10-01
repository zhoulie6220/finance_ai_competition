"""跑主张 × 事实的判定，落 `claim_match`。

用法：
    python scripts/run_claim_matching.py
    python scripts/run_claim_matching.py --project p-600019

## 为什么需要这个脚本

`app/skills/matching.py::match_and_store` 早就写好了，测试也盯着它，
但**它一个调用者都没有**——`grep -rn "match_and_store" backend/` 只找得到
定义那一行。于是：

  · `claim_match` 表**一行都没有**，「叙事一致性」页的判定表永远是空的
  · 页面上看起来像「这个功能没做」，实际是**接线断了**

## 和指数的关系（容易搞混，所以说清楚）

`/api/projects/{id}/narrative/index` **不读 `claim_match`**——
它调 `project_index_input`，从 `claim` + `claim_indicator` + `financial_fact`
**现算**一遍 H 与 C。

所以指数页有数字、判定表却是空的，这两件事**同时成立**。
判定表的价值在于「点开看每一条为什么这么判」——
`claim_match` 存的是判定理由与偏差，是复核的入口。

⚠ 代价是同一套 `judge()` 现在跑两遍。两边的输入取自同一批行，
所以结论一致；但如果哪天有人只改了其中一处取数，两边就会分叉，
而**页面上看不出哪个是旧的**。改动取数逻辑时两处一起改。
"""

from __future__ import annotations

import argparse
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

from app.db import repository  # noqa: E402
from app.db.session import connect  # noqa: E402
from app.skills import matching  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description="跑主张—事实判定")
    parser.add_argument(
        "--project", action="append", default=None,
        help="只跑这个项目（可重复）；不给则所有有主张的项目都跑",
    )
    args = parser.parse_args()

    con = connect(DB_PATH)
    try:
        projects = args.project or [
            r[0] for r in con.execute(
                "SELECT DISTINCT project_id FROM claim ORDER BY 1"
            )
        ]
        if not projects:
            print(
                "✗ 库里没有任何主张（claim 是空的）。\n"
                "  先跑：python scripts/run_rule_extraction.py"
            )
            return 1

        now = _utc_now()
        print("主张 × 事实判定（纯函数，不联网、不需要密钥）")
        print()

        for pid in projects:
            name = repository.get_project(con, pid)
            label = name["company_name"] if name else pid
            summary = matching.match_and_store(con, pid, now=now)
            con.commit()

            print(f"  {label}（{pid}）")
            print(f"    {summary.describe()}")
            print(
                f"    进 H 的观测 {len(summary.history_scores)} 个，"
                f"进 C 的观测 {len(summary.current_scores)} 个"
            )
            for w in summary.warnings:
                print(f"    ⚠ {w}")
            print()

        total = con.execute("SELECT COUNT(*) FROM claim_match").fetchone()[0]
        print(f"✓ 完成。claim_match 表现有 {total} 行")
        print()
        print("  下一步——看指数：")
        print("      uvicorn app.main:app --reload")
        print("      GET /api/projects/{id}/narrative/matches   （逐条判定与理由）")
        print("      GET /api/projects/{id}/narrative/index     （诊断指数）")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
