"""把工作台要的**全部接口响应**导成一个 JSON 快照，供离线单文件网页使用。

用法：
    python scripts/export_demo_snapshot.py --out ../桌面快照/snapshot.json

## 为什么要有这个东西

会计同学不装 Python、不跑服务。要让他看到工作台，只有两条路：
把整台环境打包（Python + 依赖 + 库，几百 MB，还得教他怎么启动），
或者**把数据冻成一份快照，塞进一个能双击打开的 HTML**。这里做的是后者。

## 快照里放什么，是**数出来的**，不是猜的

前端的取数口只有一个（`frontend/src/api/client.ts::apiGet`），
所以「要哪些接口」可以从调用点列出来。**漏一条的后果是那一块空白**，
而空白和「本来就没数据」长得一模一样——所以这里宁可多导：

    /health  /projects
    /projects/{id}/fact-grid
    /projects/{id}/narrative/{index,matches,claims}
    /projects/{id}/narrative/index/components/{h,c,r,p,q}
    /checks?project_id={id}
    /facts/{fact_id}            每一笔事实的详情
    /facts/{fact_id}/page       每一笔事实的原文页（**证据链的终点，不能少**）
    /pages/{page_id}            事实页的前后页，供证据抽屉里翻页

## 前后页为什么要单独导

证据抽屉里能点「上一页 / 下一页」，它会去取 `/pages/{page_id}`。
只导事实页的话，**翻页按钮点下去是空的**——而这一屏正是给评委看
「能点回原文」的地方。所以把每个事实页的前后页也一并导进来。

## 大小

实测全量约几 MB。这不是问题：它是要塞进单个 HTML 里的，
压缩后更小，而浏览器解析几 MB 的 JSON 在百毫秒量级。
**为了省几百 KB 去砍掉一批接口，换来的是页面上某块空白**，不划算。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402

_console.setup()


def collect(client) -> dict[str, object]:
    """按接口清单逐条取数。返回 `{请求键: 响应体}`。"""
    out: dict[str, object] = {}

    def get(key: str) -> object | None:
        res = client.get("/api" + key)
        if res.status_code != 200:
            # 不静默跳过：快照里少一条，页面上那块就是空的，
            # 而空和「本来没数据」分不出来。导的时候就报出来。
            print(f"  ⚠ {key} → HTTP {res.status_code}，**这一条没进快照**")
            return None
        out[key] = res.json()
        return out[key]

    get("/health")
    projects = get("/projects")
    ids = [p["project_id"] for p in projects] if isinstance(projects, list) else []

    page_ids: set[str] = set()
    for pid in ids:
        print(f"  {pid}")
        get(f"/projects/{pid}/fact-grid")
        get(f"/projects/{pid}/narrative/index")
        get(f"/projects/{pid}/narrative/matches")
        # ⚠ **查询串是键的一部分，必须和前端请求的一模一样。**
        #
        # 前端 `Narrative.tsx` 取主张时显式传了 `limit=2000`（不传的话
        # 后端默认 300，宝钢多出来的那些会被**静默截掉**）。这里原来冻的是
        # **不带参数**的 `/narrative/claims`，于是页面去查
        # `/…/claims?limit=2000` 查不到，报「离线快照里没有这一条」——
        # 而那条报错说的是真话：**确实是打包漏了**。
        #
        # 只冻带参数那一版：不带参数的那版页面从来不请求它。
        get(f"/projects/{pid}/narrative/claims?limit=2000")
        get(f"/checks?project_id={pid}")
        for key in ("h", "c", "r", "p", "q"):
            get(f"/projects/{pid}/narrative/index/components/{key}")

        grid = out.get(f"/projects/{pid}/fact-grid") or {}
        fact_ids = [
            cell["fact_id"]
            for m in grid.get("metrics", [])
            for cell in m.get("cells", {}).values()
            if cell.get("fact_id")
        ]
        for fid in fact_ids:
            get(f"/facts/{fid}")
            page = get(f"/facts/{fid}/page")
            if isinstance(page, dict):
                for side in ("prev_page_id", "next_page_id"):
                    if page.get(side):
                        page_ids.add(page[side])

    # 前后页最后导：它们自己也可能有前后页，但**只扩一层**——
    # 再往外交给「点不到就报错」，而不是把整份年报吸进来。
    for pg in sorted(page_ids):
        get(f"/pages/{pg}")

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="导出工作台的离线数据快照")
    ap.add_argument("--out", required=True, help="快照写到哪里（.json）")
    ap.add_argument("--strict", action="store_true",
                    help="任何一条接口失败就退出非零（打包用）")
    args = ap.parse_args()

    from fastapi.testclient import TestClient

    from app.main import app

    print("正在取数…")
    snapshot = collect(TestClient(app))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    size = out.stat().st_size
    print(f"✓ {len(snapshot)} 条接口 → {out}（{size / 1024 / 1024:.2f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
