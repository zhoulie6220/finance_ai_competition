"""用模型给 P 表做预判（只出候选，不定分）。

用法：
    python scripts/predict_p_confirmations.py
    python scripts/predict_p_confirmations.py --project p-600019
    python scripts/predict_p_confirmations.py --project p-600019 --limit 20   # 先试一小批

跑完重导 CSV（在仓库根目录）：

    python scripts/export_input_templates.py --export

## 它做什么、不做什么

做：逐条判「是不是实质经营表述 / 是不是套话 / 缺哪些要素」，落 `p_prediction`，
    导出时预填进 CSV 并带 `预判` 标记列。

**不做：`conclusion` 一律留空。** 会计口径原话是「模型只能提出候选，
不能自动定 P」——P 的分子分母只认 `p_confirmation`（会计签过字的那张表）。
模型碰不到分数。

## 为什么要先录 cassette

跑一次要几十次调用。录下来之后，**断网也能重跑**，演示和复核都不依赖网络。
录制的开关是 `.env` 的 `OFFLINE_MODE`：
    0 = 实时调用，**顺手录制**
    1 = 只走预录回放，一个字节不出网
`RecordingTransport` 会先查有没有同一输入的磁带，有就直接放——
所以重跑只花「还没录过的那部分」。
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
from app.skills import p_predict  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "var" / "finance.db"


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description="给 P 表做模型预判")
    parser.add_argument(
        "--project", action="append", default=None,
        help="只跑这个项目（可重复）；不给则所有有候选的项目都跑",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="每个项目最多预判几条（先试一小批看效果用）",
    )
    args = parser.parse_args()

    con = connect(DB_PATH)
    try:
        projects = args.project or [
            r[0] for r in con.execute(
                "SELECT DISTINCT project_id FROM claim WHERE verifiable = 1"
                " ORDER BY 1"
            )
        ]
        if not projects:
            print(
                "✗ 库里没有可验证的主张。\n"
                "  先跑：python scripts/parse_mdna.py &&"
                " python scripts/run_rule_extraction.py"
            )
            return 1

        from app.agents.llm.settings import LlmSettings

        settings = LlmSettings.from_env()
        print(f"预判 P 表候选（{settings.describe()}）")
        print("⚠ 只出候选，**不填 conclusion**——定分仍需会计逐条确认")
        print()

        for pid in projects:
            row = repository.get_project(con, pid)
            label = row["company_name"] if row else pid
            summary = p_predict.predict_for_project(
                con, pid, now=_utc_now(), limit=args.limit
            )
            con.commit()
            print(f"  {label}（{pid}）")
            print(f"    {summary.describe()}")
            for w in summary.warnings[:4]:
                print(f"    ⚠ {w[:96]}")
            print()

        total = con.execute("SELECT COUNT(*) FROM p_prediction").fetchone()[0]
        print(f"✓ 完成。p_prediction 表现有 {total} 行")
        print()
        print("  下一步——把预判预填进 CSV：")
        print("      python scripts/export_input_templates.py --export")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
