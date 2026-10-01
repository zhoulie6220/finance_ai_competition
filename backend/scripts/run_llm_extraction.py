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

# _console 与本文件同目录。**这一行不能省**：
# 直接 `python scripts/x.py` 时 Python 会自动把脚本目录放进 sys.path，
# 但测试用 `spec_from_file_location` 按路径加载脚本时**不会**——
# 少了它，`import _console` 只在跑测试时炸，看起来像测试坏了。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402  (与本文件同目录)

_console.setup()

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

        # ★ **先把待抽的段 id 一次取全，再按批传进去。**
        #
        # ⚠ 不能用「`limit=批大小` + `only_missing`」循环：`only_missing` 挑的是
        # 「这一段下还没有 LLM 主张的」，而**有些段抽出来就是零条**——模型认为
        # 整段没有可验证表述，或者没有主判据指标。那些段永远进不了「已完成」，
        # 于是**每一批都会被重新挑中、重新调一次**：
        #
        #   · 白花调用的钱（实测第一批 43 次调用只覆盖了 33 段）
        #   · 进度看着在走（`done` 按批大小加），实际覆盖的段远没那么多
        #   · **录制的 cassette 被同一批 prompt 反复覆写，文件数一直不涨**，
        #     看起来像「录制根本没生效」
        #
        # 取一次 id 列表就没有这个问题：每段至多被处理一次，
        # 抽不出东西的段也只付一次代价。
        con = connect(DB_PATH)
        try:
            pending = _remaining_section_ids(con, project)
        finally:
            con.close()

        done = 0
        for start in range(0, len(pending), args.batch):
            if spent >= args.max_tokens:
                print()
                print(f"⚠ 已达 token 预算上限 {args.max_tokens:,}，主动停下。")
                print("  已抽的部分是完整的（每段各自成事务）；重跑时会跳过它们。")
                return 0

            batch = tuple(pending[start : start + args.batch])
            con = connect(DB_PATH)
            try:
                s = extract_claims_llm(
                    con, project, section_ids=batch, only_missing=True
                )
            finally:
                con.close()

            done = start + len(batch)
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


_PENDING_SQL = """
    SELECT m.section_id FROM mdna_section m
    JOIN file f ON f.file_id = m.file_id
    WHERE f.project_id = ?
      AND NOT EXISTS (
          SELECT 1 FROM claim c
          WHERE c.section_id = m.section_id AND c.extractor LIKE 'llm:%'
      )
    ORDER BY f.period, m.page_from, m.section_id
"""


def _remaining_section_ids(con, project: str) -> list[str]:
    """还没抽到 LLM 主张的段的 id，**顺序稳定**。

    顺序稳定是要紧的：调用方按它切片分批，顺序一变，同一段可能落进两批，
    也可能一批都没落进。

    ⚠ 判据是「这一段下有没有 LLM 主张」，所以**抽出来是零条的段永远在这张表里**。
    调用方必须把这份列表**一次取全**再分批，不能每批重新查一次——
    那会让零条的段被反复挑中（见 main() 里的说明）。
    """
    return [r[0] for r in con.execute(_PENDING_SQL, (project,))]


def _remaining(con, project: str) -> int:
    """还有多少段没抽过（只用于开工前的预估，循环里不要再用它）。"""
    return len(_remaining_section_ids(con, project))


if __name__ == "__main__":
    raise SystemExit(main())
