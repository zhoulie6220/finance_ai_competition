"""跑规则法主张抽取（不需要密钥、不需要联网）。

用法：
    python scripts/run_rule_extraction.py
    python scripts/run_rule_extraction.py --project p-600019

## 为什么需要这个脚本

`app/skills/narrative.py::extract_and_store` 早就写好了，测试也一直盯着它，
但**它一个调用者都没有**——只有 `run_llm_extraction.py`（模型版）有 CLI。
于是文档里承诺的那条兜底路径：

    「模型不可用（没配密钥、断网、超时）时系统仍然能出主张，
      而不是整条链路瘫掉」

**实际触发不了**。两个后果：

  · 没配密钥就一条主张都没有，叙事一致性页是空的，**演示做不下去**
  · `by_extractor`（「规则法 vs 模型法」并排对照，验收标准里明写的那条）
    只有一边，对照不起来

## 和模型版的关系

**并存，不是替代。** 两边抽到同一句话会存成两条，靠 `extractor` 区分：

    rule:claim_v1                      本脚本（确定性，可复现）
    llm:deepseek-chat@<prompt_hash>    run_llm_extraction.py

## 幂等

`claim_id` 是 `cl-` + sha1(project_id|section_id|句子序号|归一化文本)[:12]，
落库走 `INSERT OR IGNORE`。重跑不会产生重复主张——**这是刻意的**，
因为没有唯一索引兜底，防重复全靠这个确定性 id。
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

from app.db import repository  # noqa: E402
from app.db.session import connect  # noqa: E402
from app.skills import narrative  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description="跑规则法主张抽取")
    parser.add_argument(
        "--project", action="append", default=None,
        help="只跑这个项目（可重复）；不给则所有有正文的项目都跑",
    )
    args = parser.parse_args()

    con = connect(DB_PATH)
    try:
        projects = args.project or [
            r[0] for r in con.execute(
                "SELECT DISTINCT f.project_id FROM mdna_section m"
                " JOIN file f ON f.file_id = m.file_id ORDER BY 1"
            )
        ]
        if not projects:
            print(
                "✗ 库里没有任何正文（mdna_section 是空的）。\n"
                "  先跑：python scripts/parse_mdna.py"
            )
            return 1

        before = con.execute("SELECT COUNT(*) FROM claim").fetchone()[0]
        now = _utc_now()
        print(f"规则法抽取（extractor = {narrative.EXTRACTOR}），不联网、不需要密钥")
        print()

        inserted = skipped = 0
        for pid in projects:
            row = con.execute(
                "SELECT COUNT(*) FROM mdna_section m JOIN file f ON f.file_id = m.file_id"
                " WHERE f.project_id = ?",
                (pid,),
            ).fetchone()
            name = repository.get_project(con, pid)
            label = name["company_name"] if name else pid
            summary = narrative.extract_and_store(con, pid, now=now)
            con.commit()

            inserted += summary.claims_inserted
            skipped += summary.claims_skipped_existing
            themes = "、".join(f"{k} {v}" for k, v in summary.by_theme.items())
            print(f"  {label}（{pid}）  扫描 {row[0]} 段")
            print(f"    新入库 {summary.claims_inserted} 条，已存在 {summary.claims_skipped_existing} 条")
            print(f"    主题分布：{themes or '—'}")
            print(f"    不可验证 {summary.unverifiable} 条（标 background_only，不进指数分母）")
            print()

        after = con.execute("SELECT COUNT(*) FROM claim").fetchone()[0]
        print(f"✓ 完成。本次新增 {inserted} 条，跳过已存在 {skipped} 条")
        print(f"  claim 表：{before} → {after}")
        print()
        print("  下一步（可选）——把结果喂进匹配与指数：")
        print("      python scripts/run_llm_extraction.py      # 模型版，用来做并排对照")
        print("      然后在页面上看「叙事一致性」，或直接调 /api/projects/{id}/narrative/index")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
