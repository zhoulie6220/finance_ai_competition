"""工作流技能。4 个主流程 Skill + 1 个质量控制 Skill。

已落地两个：

    selfcheck  系统自检。不依赖年报材料，用来验证「一句话 → 任务 → 工具调用 →
               结构化结果 → 时间线」这条链路本身是通的。
    facts      财务事实（读盘）。读 v_fact_verified 出「指标 × 期间」的序列与同比。
               配合 `scripts/seed_demo.py` 的演示数据，**前端可以先跑起来**——
               不必等 PDF 解析做完。

后面几个往同一套契约上长即可：

    parsing    解析年报、提取字段、落库          ← 甲
    accounting 会计校验与趋势分析                ← 乙
    diagnose   MD&A 主张抽取与诊断指数           ← 乙
    valuation  三情景估值与敏感性                ← 乙
    audit      研究报告纠错质控（决赛）          ← 乙

⚠ `can_handle` 之间的关键词不能重叠：路由是**按 ALL_SKILLS 的顺序取第一个命中**，
  两个 Skill 都认「检查」的话，后一个永远轮不到，而且不会有任何提示。

新增一个 Skill 只需两步：实现 `app.skills.base.Skill` 协议，然后加进 `ALL_SKILLS`。
**不需要改编排器** —— 编排器只认协议，不认具体 Skill。
"""

from __future__ import annotations

from app.skills.base import REGISTRY, Skill
from app.skills.facts import SKILL as FACTS
from app.skills.selfcheck import SKILL as SELFCHECK

ALL_SKILLS: tuple[Skill, ...] = (SELFCHECK, FACTS)

for _skill in ALL_SKILLS:
    REGISTRY.register(_skill)


def load_skills() -> list[Skill]:
    """返回已登记的 Skill。

    留这个函数是为了让 `app/main.py` 有一个显式的「把 Skill 装进来」的动作：
    只依赖 import 副作用的话，将来按需加载（比如决赛时把质控 Skill 拆出去）
    就会变成「删掉一行 import，功能静默消失」。
    """
    return REGISTRY.all()


__all__ = ["ALL_SKILLS", "REGISTRY", "Skill", "load_skills"]
