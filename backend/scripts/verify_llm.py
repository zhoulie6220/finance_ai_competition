"""验证 LLM 链路：真发一次请求、拿到结构化 JSON、把响应对录成 cassette。

用法：
    python scripts/verify_llm.py              # 真发请求并录制
    python scripts/verify_llm.py --replay     # 只回放已有的 cassette（不联网）

这是甲阶段一验收标准「能发一次请求、拿到结构化输出」的**可重复验证方式**。
写在脚本里而不是手工点一次，是因为验收那天需要能当场再跑一遍。

三件事一起做
------------
1. **真的发一次请求**，确认密钥、网络、模型名、接口地址都对
2. **确认拿到的是结构化 JSON**，不是一段散文
3. **把响应对录成 cassette**，之后断网演示和单测都走它

**失败路径也留痕**：`llm_call` 表里失败与成功的调用都落库，
证据链不会恰好在最需要它的时候（出错那次）断掉。
"""

from __future__ import annotations

import argparse
import json
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

from app.agents.llm.client import CallRecord, LlmClient  # noqa: E402
from app.agents.llm.prompts.registry import PromptRegistry  # noqa: E402
from app.agents.llm.settings import LlmSettings  # noqa: E402
from app.agents.llm.transport import build_transport  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = BACKEND_DIR / "app" / "agents" / "llm" / "prompts"

#: 一段真实的宝钢 MD&A 原文。**用真句子而不是造的例句**——
#: 造的例句测不出「模型在真实年报文本上表现如何」。
SAMPLE_TEXT = """2022年，公司克服疫情和钢铁市场下行等多重压力和挑战，深化一公司多基地
管控模式，持续加大全面对标找差，加大购销协同力度，动态优化资源流向和产品结构，
加大降本控费挖潜力度。全年实现"1+1+N"产品销量2538万吨，销量同比上升183万吨。
报告期内公司销售商品坯材4,976.3万吨，实现营业总收入3,690.6亿元。
"""

SAMPLE_VARS = {
    "metric_keys": "steel_sales_volume（钢材销量）\noperating_cost（营业成本）\nrevenue（营业收入）",
    "period": "2022",
    "section_heading": "经营情况讨论与分析",
    "section_text": SAMPLE_TEXT.strip(),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 LLM 链路")
    parser.add_argument("--replay", action="store_true", help="只回放，不联网")
    args = parser.parse_args()

    settings = LlmSettings.from_env()
    if args.replay:
        # 回放模式：不碰网络，也不需要密钥
        from dataclasses import replace

        settings = replace(settings, offline_mode=True)

    print(f"模型      {settings.model}")
    print(f"服务地址  {settings.base_url}")
    print(f"状态      {settings.describe()}")
    print(f"Prompt 目录 {PROMPTS_DIR}")
    print()

    if not settings.offline_mode and not settings.configured:
        print("✗ 没配密钥。填 backend/.env 的 LLM_API_KEY，或加 --replay 走回放。",
              file=sys.stderr)
        return 1

    registry = PromptRegistry(PROMPTS_DIR)
    print(f"已加载 prompt：{'、'.join(registry.keys())}")
    print(f"注册表哈希：{registry.registry_hash()[:16]}…")
    print()

    calls: list[CallRecord] = []
    transport = build_transport(
        offline_mode=settings.offline_mode,
        base_url=settings.base_url,
        api_key=settings.api_key,
        cassette_dir=settings.cassette_dir,
        record=not settings.offline_mode,   # 实时调用时顺手录制
    )
    client = LlmClient(
        settings=settings,
        transport=transport,
        prompts=registry,
        sink=calls.append,
    )

    def validate(data: dict) -> None:
        """校验结构化输出。

        ⚠ 不通过时要抛异常——那是 schema_repair 重试的触发条件。
        只打印一句警告然后放行的话，坏数据会一路流到下游，
        而下游拿到的是一个「结构对不上但看起来正常」的 dict。
        """
        from app.agents.llm.client import SchemaError

        claims = data.get("claims")
        if not isinstance(claims, list):
            raise SchemaError(f"claims 必须是数组，收到 {type(claims).__name__}")
        for i, c in enumerate(claims):
            if not isinstance(c, dict):
                raise SchemaError(f"claims[{i}] 不是对象")
            for field in ("text", "direction", "claim_type", "verifiable"):
                if field not in c:
                    raise SchemaError(f"claims[{i}] 缺字段 {field}")

    print("正在发请求…")
    from app.agents.llm.transport import TransportError

    try:
        result = client.complete_json(
            purpose="claim_extract",
            prompt_key="claim_extract",
            variables=SAMPLE_VARS,
            validator=validate,
        )
    except TransportError as exc:
        # 传输层失败（网络、鉴权、超时）。**先打印调用记录**——
        # 失败路径也落了库，证据链不该恰好在最需要它的时候断掉。
        print()
        print(f"调用记录 {len(calls)} 次：")
        for c in calls:
            print(f"  ✗ 第 {c.attempt + 1} 次  {c.status}  {c.error and c.error[:120]}")
        print()
        _explain(exc)
        return 1
    print()

    # ---- 每次调用都落库，失败路径也要 ----
    print(f"调用记录 {len(calls)} 次：")
    for c in calls:
        mark = "✓" if c.status == "succeeded" else "✗"
        print(
            f"  {mark} 第 {c.attempt + 1} 次  {c.status:9}  "
            f"{c.latency_ms or 0:>5} ms  "
            f"prompt={c.prompt_key}@{c.prompt_version}  "
            f"in={c.tokens_in} out={c.tokens_out}"
        )
        if c.error:
            print(f"     错误：{c.error[:100]}")
        # 每次都在检查：密钥绝不能出现在落库内容里
        assert "sk-" not in json.dumps(c.params, ensure_ascii=False), "params 里出现了密钥！"

    print()
    print(f"params 里不含密钥 ✓（{sorted(calls[0].params)}）")

    if not result.ok:
        print()
        print("✗ 重试用尽仍未拿到合法 JSON。", file=sys.stderr)
        print(f"  最后一次的原始输出：{(calls[-1].output or '')[:200]!r}", file=sys.stderr)
        return 1

    claims = result.data.get("claims", []) if result.data else []
    print()
    print(f"✓ 拿到结构化 JSON：{len(claims)} 条主张"
          f"{'（经过一次 schema 修复）' if result.repaired else ''}")
    print()
    for c in claims[:5]:
        print(f"  · [{c.get('claim_type')}/{c.get('direction')}] "
              f"{str(c.get('text'))[:60]}")
        print(f"      主判据 {c.get('metric_key')}  "
              f"幅度 {c.get('magnitude_text')}  置信度 {c.get('confidence')}")
    if len(claims) > 5:
        print(f"  …还有 {len(claims) - 5} 条")

    print()
    if settings.offline_mode:
        print("（回放模式，未联网）")
    else:
        print(f"✓ 响应已录成 cassette → {settings.cassette_dir}")
        print("  之后 OFFLINE_MODE=1 或 --replay 都不再需要联网，也不需要密钥。")
    return 0


def _explain(exc: Exception) -> None:
    """把传输层的错误翻译成「下一步该做什么」。

    只把原始异常打出来是不够的——`401 Authentication Fails` 这句话
    不会告诉一个不熟悉 API 的人该去检查什么。
    """
    text = str(exc)
    print("✗ 请求发出去了，但模型方拒绝了：")
    print(f"  {text[:200]}")
    print()

    if "401" in text or "Authentication" in text or "invalid" in text:
        print("这是**密钥无效**。按顺序检查：")
        print()
        print("  1. key 复制全了吗？")
        print("     DeepSeek 的 key 是 sk- 加 32 位，一共 35 个字符。")
        print("     复制时少一位就会报这个错，而且看不出少了。")
        print()
        print("  2. key 是不是在 platform.deepseek.com 建的？")
        print("     （不是别的平台的）")
        print()
        print("  3. 那个 key 在平台上还在吗？")
        print("     已经被删掉的 key 用起来就是这个报错。")
        print()
        print("  4. 账号里有余额吗？")
        print("     DeepSeek 是按量付费的，余额为 0 时调用会被拒。")
        print()
        print("  最快的办法：回 platform.deepseek.com，「API keys」里")
        print("  **新建一个** key（创建时点复制），替换 backend/.env 第 5 行。")
        print("  注意旧的 key 只在创建那一刻显示一次，页面上看不到。")
    elif "timeout" in text.lower():
        print("这是**超时**。检查网络，或把 backend/.env 里的")
        print("LLM_TIMEOUT_S 调大（默认 60 秒）。")
    elif "balance" in text.lower() or "quota" in text.lower():
        print("这是**余额或配额问题**。去 platform.deepseek.com 充值。")
    else:
        print("这是传输层错误。检查网络连通性，或稍后重试。")


if __name__ == "__main__":
    raise SystemExit(main())
