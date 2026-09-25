"""规则路由的行为钉死在这里。

## 为什么值得专门一个测试文件

路由错了**不报错**。用户问「管理层说的话和财务事实对得上吗」，
被路由到「财务事实」Skill，页面上照样出来三张图表、时间线照样逐条亮起、
任务照样标记为「已完成」——他看到了一份看起来很完整的答案，
只是答的是另一个问题。

这跟 CLAUDE.md 里反复出现的那类缺陷是同一族：**静默走错分支**。
唯一能挡住它的东西就是这类测试。

## 当前路由的真实约束

`SkillRegistry.route()` 按 `all()` 的顺序取**第一个命中**，而 `all()` 是
按 key 排序的，于是顺序固定为：facts → narrative → selfcheck。

后果：「事实」这两个字是 `facts` 的关键词，而它又是叙事类提问里的高频词。
两边只要同时命中，facts 一定赢。
"""

from __future__ import annotations

import sys

import pytest

from app.skills import REGISTRY, load_skills
from app.skills.base import SkillRequest

load_skills()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 财务事实：靠指标名与「趋势」这类词
        ("看一下这家公司的财务事实趋势", "facts"),
        ("营收和利润怎么样", "facts"),
        ("毛利率多少", "facts"),
        # 叙事一致性：靠「叙事」「管理层」「措辞」这类词
        ("管理层说的话和财务数字对得上吗", "narrative"),
        ("看下叙事一致性", "narrative"),
        ("管理层有没有吹牛", "narrative"),
        ("这些承诺兑现了没有", "narrative"),
        # 自检
        ("跑一次系统自检", "selfcheck"),
    ],
)
def test_representative_inputs_route_to_the_expected_skill(text: str, expected: str) -> None:
    skill = REGISTRY.route(SkillRequest(user_input=text))
    assert skill is not None, f"{text!r} 没有命中任何 Skill"
    assert skill.key == expected, (
        f"{text!r} 被路由到了 {skill.key}，期望 {expected}。"
        "规则路由按关键词命中，撞车时排在前面的赢——"
        "改词表时务必回来跑这个测试。"
    )


def test_the_demo_button_labels_route_correctly() -> None:
    """前端那三个预设按钮的文案，逐条对一遍。

    它们是演示时最先被点的东西，而文案本身受关键词约束
    （见 `frontend/src/App.tsx` 里 PRESETS 上方的注释）。
    改文案而不过这个测试，演示当天就会走到错的分支。
    """
    presets = {
        "看一下这家公司的财务事实趋势": "facts",
        "管理层说的话和财务数字对得上吗": "narrative",
        "跑一次系统自检": "selfcheck",
    }
    for text, expected in presets.items():
        skill = REGISTRY.route(SkillRequest(user_input=text))
        assert skill is not None and skill.key == expected, (
            f"预设按钮 {text!r} 路由到了 {skill and skill.key}，期望 {expected}"
        )


def _keywords_of(skill: object) -> tuple[str, ...]:
    """取一个 Skill 的关键词。

    ⚠ 关键词是**模块级**变量（`_KEYWORDS = (...)`），不是类属性——
    `facts` 与 `selfcheck` 都这么写。所以要从 `sys.modules` 里找它所属的模块，
    不能 `getattr(skill, ...)`：那样拿到的是 None，而测试会以为「这个 Skill
    没有关键词」，报出一条和真实情况无关的错。
    """
    mod = sys.modules[type(skill).__module__]
    return tuple(getattr(mod, "_KEYWORDS", ()))


def test_no_skill_keyword_is_a_substring_of_another_skills_keyword() -> None:
    """跨 Skill 的关键词不得互相包含。

    同一条规则在 `app/skills/__init__.py` 的模块注释里写着，这里把它变成
    能跑的东西。包含关系是最难查的一种撞车：两条关键词各自看着都对，
    只有把它们摆在一起才发现一个永远轮不到。
    """
    skills = REGISTRY.all()
    for a in skills:
        for b in skills:
            if a.key == b.key:
                continue
            for ka in _keywords_of(a):
                for kb in _keywords_of(b):
                    assert ka not in kb, (
                        f"{a.key} 的关键词 {ka!r} 是 {b.key} 的关键词 {kb!r} 的子串，"
                        "两个 Skill 会互相抢"
                    )


def test_every_skill_declares_keywords() -> None:
    """每个 Skill 都得有关键词，否则 `can_handle` 无从判断。

    这个断言看起来废话，但它挡的是「复制一个 Skill 改了类名忘了改关键词」——
    那样新 Skill 会永远命中不到，而表现是「这个功能没反应」，没人会往关键词上想。
    """
    for skill in REGISTRY.all():
        assert _keywords_of(skill), f"Skill {skill.key} 没有声明模块级 _KEYWORDS"
