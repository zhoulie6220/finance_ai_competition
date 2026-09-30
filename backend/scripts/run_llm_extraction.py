"""批量跑 LLM 主张抽取。

用法：
    python scripts/run_llm_extraction.py --project p-600019
    python scripts/run_llm_extraction.py                 # 三个公司都跑
    python scripts/run_llm_extraction.py --max-tokens 600000

## 为什么要有 token 上限

模型调用是**按量付费**的，而这一跑是几百次调用。跑到一半余额耗尽的话，
后面那些章节就一直失败——数据不是坏了（每一段各自成事务），
但会留下一批「这个公司只抽了一半」的结果，而**从数据上看不出来**。

所以给一个预算上限，快到时**主动停下并说明**，而不是撞到余额墙。

## 幂等

已有的同 id 主张不会被覆盖（`INSERT OR IGNORE`），所以中断之后
直接重跑即可——已经抽过的段落会被跳过，只补没跑完的。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import connect  # noqa: E402
from app.skills.narrative_llm import extract_claims_llm  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"

#: 单次调用的大致 token 量（实测：in≈2100 + out≈1600）。
#: 用来预估「还能跑几段」——只是估算，真实用量以返回值为准。
TOKENS_PER_CALL_ESTIMATE = 3700


def main() -> int:
    parser = argparse.ArgumentParser(description="批量跑 LLM 主张抽取")
    parser.add_argument("--project", action="append", default=None,
                        help="只跑这个项目（可重复）；不给则三个都跑")
    parser.add_argument("--max-tokens", type=int, default=700_000,
                        help="token 预算上限，快到时主动停下（默认 70 万）")
    parser.add_argument("--batch", type=int, default=20,
                        help="每次跑几段就报一次进度（默认 20）")
    args = parser.parse_args()

    con = connect(DB_PATH)
    try:
        projects = args.project or [
            r[0] for r in con.execute(
                "SELECT DISTINCT f.project_id FROM mdna_section m"
                " JOIN file f ON f.file_id = m.file_id ORDER BY 1"
            )
        ]
        todo = {p: _remaining(con, p) for p in projects}
    finally:
        con.close()

    total = sum(todo.values())
    print(f"待抽取 {total} 段：")
    for p, n in todo.items():
        print(f"  {p}  {n} 段")
    print()
    print(f"token 预算 {args.max_tokens:,}（按每段约 {TOKENS_PER_CALL_ESTIMATE:,} 估算，"
          f"大约能跑 {args.max_tokens // TOKENS_PER_CALL_ESTIMATE} 段）")
    print()

    spent = 0
    started = time.monotonic()

    for project, remaining in todo.items():
        if remaining == 0:
            print(f"—— {project}：已抽完，跳过 ——")
            continue

        print(f"—— {project}（{remaining} 段）——")
        done = 0
        while done < remaining:
            if spent >= args.max_tokens:
                print()
                print(f"⚠ 已达 token 预算上限 {args.max_tokens:,}，主动停下。")
                print("  已抽的部分是完整的（每段各自成事务）；重跑时会跳过它们。")
                return 0

            step = min(args.batch, remaining - done)
            con = connect(DB_PATH)
            try:
                s = extract_claims_llm(con, project, limit=step, only_missing=True)
            finally:
                con.close()

            done += step
            spent += s.calls * TOKENS_PER_CALL_ESTIMATE

            elapsed = time.monotonic() - started
            print(
                f"  {done:>4}/{remaining}  入库 {s.claims_inserted:>3}"
                f"  剔除表格 {s.table_rows:>3}"
                + (f"  原句找不到 {s.not_in_source}" if s.not_in_source else "")
                + f"   [{elapsed:.0f}s, 约 {spent / 1000:.0f}K tokens]"
            )
            if s.sections_failed:
                for w in s.warnings[:2]:
                    print(f"      ⚠ {w[:90]}")
            if s.claims_inserted == 0 and s.sections_failed == step:
                print("      整批失败，停下——多半是余额、网络或密钥的问题。")
                return 1
        print()

    elapsed = time.monotonic() - started
    print(f"✓ 全部跑完，用时 {elapsed / 60:.1f} 分钟，约 {spent / 1000:.0f}K tokens")
    print("  下一步：python scripts/refresh_narrative.py 重新跑匹配与指数")
    return 0


def _remaining(con, project: str) -> int:
    """还有多少段没抽过。

    判据是「这一段的章节下有没有 LLM 抽出来的主张」——
    已经抽过的段落重跑会被 `INSERT OR IGNORE` 跳过，但白白花一次调用的钱。
    """
    row = con.execute(
        """
        SELECT COUNT(*) FROM mdna_section m
        JOIN file f ON f.file_id = m.file_id
        WHERE f.project_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM claim c
              WHERE c.section_id = m.section_id AND c.extractor LIKE 'llm:%'
          )
        """,
        (project,),
    ).fetchone()
    return row[0]


if __name__ == "__main__":
    raise SystemExit(main())
