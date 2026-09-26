"""演示动线冒烟测试：按**演示顺序**打一遍所有接口。

用法：
    python scripts/smoke_demo.py            # 跑一遍
    python scripts/smoke_demo.py --times 3  # 连跑三遍（每个截止日前的固定动作）

为什么需要它
------------
「连录 3 次不挂」是阶段二的验收标准。靠人肉演示三遍来验的话，
第一遍看到的是内容、第二遍开始走神、第三遍基本在等它跑完——
中途一个接口 500 或者返回空数组，很可能就滑过去了。

把它写成脚本之后，「不挂」从一句口号变成一条命令。
而且它按**演示的先后顺序**打，所以顺序上的依赖（比如先选公司才能看网格）
也会被覆盖到。

**不用 TestClient**：TestClient 走的是进程内调用，绕过了真正的 HTTP 层、
CORS、代理和 uvicorn。演示时走的恰恰是那一层。

退出码
------
0   全部通过
1   任一步失败——**不要进入正式录制**
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

BASE = "http://127.0.0.1:8000"

# 演示动线的主公司。可比公司在演示中只作对照，不做完整动线。
PRIMARY = "p-600019"


@dataclass
class Step:
    name: str
    path: str
    #: 至少要有多少条记录才算「有内容」。0 表示只要求 HTTP 200。
    min_items: int = 0
    #: 从响应里取数组的字段路径。空表示响应本身就是数组。
    items_key: str | None = None


@dataclass
class Result:
    passed: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


#: **顺序就是演示的顺序。** 改这里等于改演示动线。
STEPS: tuple[Step, ...] = (
    # ① 开场：系统自检（离线回放横幅由 llm_mode 驱动）
    Step("健康检查", "/api/health"),
    # ② 一句话 → 选公司
    Step("公司列表", "/api/projects", min_items=1),
    Step("公司详情", f"/api/projects/{PRIMARY}", min_items=1, items_key="files"),
    # ③ 财务事实表
    Step("财务事实网格", f"/api/projects/{PRIMARY}/fact-grid", min_items=10, items_key="metrics"),
    Step("勾稽校验", f"/api/checks?project_id={PRIMARY}", min_items=1, items_key="results"),
    # ④ 叙事一致性
    Step("诊断指数", f"/api/projects/{PRIMARY}/narrative/index"),
    Step("主张列表", f"/api/projects/{PRIMARY}/narrative/claims", min_items=1, items_key="claims"),
    Step("主张—事实对照", f"/api/projects/{PRIMARY}/narrative/matches", min_items=1, items_key="matches"),
    # ⑤ 点回原文
    Step("规则参数", "/api/rule-config", min_items=10),
)


def fetch(
    path: str, *, base: str = BASE, timeout: float = 30.0
) -> tuple[int, Any, float]:
    """打一个接口，返回 (状态码, 解析后的响应, 耗时秒)。

    `base` 显式传入而不是读模块级常量——用全局变量的话，
    CLI 改地址和函数读地址之间会隔着一层看不见的状态。
    """
    url = base.rstrip("/") + path
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001 - 网络层的异常种类很多
        raise RuntimeError(f"连不上 {url}：{exc}") from exc
    elapsed = time.monotonic() - started
    try:
        return status, json.loads(body), elapsed
    except json.JSONDecodeError:
        return status, body, elapsed


def run_once(*, base: str = BASE, verbose: bool = True) -> Result:
    result = Result()
    for step in STEPS:
        try:
            status, body, elapsed = fetch(step.path, base=base)
        except RuntimeError as exc:
            result.failed += 1
            result.failures.append(f"{step.name}：{exc}")
            if verbose:
                print(f"  ✗ {step.name:16} {exc}")
            continue

        result.durations.append(elapsed)

        if status != 200:
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("detail", ""))[:80]
            result.failed += 1
            result.failures.append(f"{step.name}：HTTP {status} {detail}")
            if verbose:
                print(f"  ✗ {step.name:16} HTTP {status}  {detail}")
            continue

        count = _count(body, step.items_key)
        if count is not None and count < step.min_items:
            result.failed += 1
            result.failures.append(
                f"{step.name}：只有 {count} 条，少于要求的 {step.min_items} 条"
            )
            if verbose:
                print(f"  ✗ {step.name:16} 内容不足（{count} < {step.min_items}）")
            continue

        result.passed += 1
        suffix = f"{count} 条" if count is not None else ""
        if verbose:
            print(f"  ✓ {step.name:16} {elapsed * 1000:6.0f} ms  {suffix}")

    return result


def _count(body: Any, items_key: str | None) -> int | None:
    if items_key is None:
        return len(body) if isinstance(body, list) else None
    if isinstance(body, dict):
        value = body.get(items_key)
        return len(value) if isinstance(value, list) else None
    return None


def check_grid_coverage(base: str = BASE) -> tuple[bool, str]:
    """每个有事实的项目，网格必须有至少一格不是 not_found。

    ⚠ 这一条单独拎出来，因为它抓的是一类**不报错**的缺陷：
    视图换了、is_primary 过滤加回来了、口径写死了——接口都会正常返回 200，
    只是每一格都是空的。页面上看起来像「年报没披露」，而实际上有数据。
    """
    problems: list[str] = []
    status, projects, _ = fetch("/api/projects", base=base)
    if status != 200 or not isinstance(projects, list):
        return False, "拿不到公司列表"

    for p in projects:
        pid = p.get("project_id")
        if not pid or not p.get("fact_count"):
            continue
        status, body, _ = fetch(f"/api/projects/{pid}/fact-grid", base=base)
        if status != 200 or not isinstance(body, dict):
            problems.append(f"{pid} 网格接口异常")
            continue
        filled = sum(
            1
            for m in body.get("metrics", [])
            for cell in (m.get("cells") or {}).values()
            if cell.get("status") != "not_found"
        )
        if filled == 0:
            problems.append(
                f"{pid}（{p.get('company_name')}）有 {p.get('fact_count')} 条事实，"
                f"但网格一格有值的都没有——多半是又换回 v_fact_grid 了"
            )
    if problems:
        return False, "；".join(problems)
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="演示动线冒烟测试")
    parser.add_argument("--times", type=int, default=1, help="连跑几遍（默认 1）")
    parser.add_argument("--base", default=BASE, help=f"后端地址（默认 {BASE}）")
    args = parser.parse_args()

    base = args.base
    print(f"演示动线冒烟测试 → {base}")
    print(f"动线 {len(STEPS)} 步，连跑 {args.times} 遍")
    print()

    overall = Result()
    for i in range(1, args.times + 1):
        print(f"—— 第 {i} 遍 ——")
        r = run_once(base=base)
        overall.passed += r.passed
        overall.failed += r.failed
        overall.failures.extend(r.failures)
        overall.durations.extend(r.durations)
        print()

        # 网格覆盖检查每遍都做：它抓的是那类静默缺陷
        ok, why = check_grid_coverage(base)
        if ok:
            print("  ✓ 网格覆盖：每个有事实的项目都有非空格子")
        else:
            overall.failed += 1
            overall.failures.append(f"网格覆盖：{why}")
            print(f"  ✗ 网格覆盖：{why}")
        print()

    if overall.durations:
        slowest = max(overall.durations)
        print(f"最慢一步 {slowest * 1000:.0f} ms（演示时单步超过 2 秒就会显得卡）")
        if slowest > 2.0:
            print("  ⚠ 有步骤超过 2 秒，演示前值得查一下")

    print()
    if overall.ok:
        print(f"✓ 全部通过（{overall.passed} 步 × {args.times} 遍）")
        return 0

    print(f"✗ {overall.failed} 步失败，**不要进入正式录制**：", file=sys.stderr)
    for f in overall.failures:
        print(f"  · {f}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
