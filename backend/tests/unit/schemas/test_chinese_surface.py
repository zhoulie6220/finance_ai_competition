"""对外可见的文案必须是中文。

这些断言看起来像在管「文档好不好看」，其实管的是**同一类静默失效**：

- 模型没有中文 title，`/docs` 和前端类型合同里就显示英文类名——不会报错，
  只是评审看到的界面上突然冒出一个 `TaskStepView`。
- 枚举用基类继承挂 title，会**凭空多出一个成员**（`list(FileRole)` 多一项），
  所有遍历枚举的代码多跑一轮且不报错。见 `app/schemas/enums.py::cn_enum` 的说明。

所以这里逐条盯着，而不是靠「记得写」。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from pydantic import BaseModel

import app.schemas


def _is_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _schema_modules():
    for info in pkgutil.iter_modules(app.schemas.__path__):
        yield importlib.import_module(f"app.schemas.{info.name}")


def _defined_classes(base: type) -> list[type]:
    """本包内**自己定义**的（不是 import 进来的）类。"""
    out: dict[str, type] = {}
    for module in _schema_modules():
        for name, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, base)
                and obj is not base
                and obj.__module__ == module.__name__
            ):
                out[f"{module.__name__}.{name}"] = obj
    return list(out.values())


@pytest.mark.parametrize(
    "model", _defined_classes(BaseModel), ids=lambda m: m.__name__
)
def test_every_model_has_a_chinese_title(model: type[BaseModel]) -> None:
    """每个模型的 title 都得是中文。

    没有 title 时 Pydantic 拿**类名**当标题，于是 `/docs` 的 Schemas 区
    和 `frontend/src/types/contract.json` 里都是 `TaskStepView` 这样的英文标识符。
    """
    title = model.model_config.get("title")
    assert title, f"{model.__name__} 没写 title，文档和前端类型里会显示英文类名"
    assert _is_chinese(title), f"{model.__name__} 的 title 不是中文：{title!r}"


def test_every_enum_has_a_chinese_title() -> None:
    """每个枚举都得挂 `@cn_enum`。"""
    from enum import Enum

    missing = [
        e.__name__ for e in _defined_classes(Enum)
        if not _is_chinese(getattr(e, "title_cn", "") or "")
    ]
    assert not missing, f"这些枚举没挂 @cn_enum：{missing}"


def test_cn_enum_does_not_add_a_member() -> None:
    """`@cn_enum` 不能把标题变成枚举成员。

    这条是**反例测试**：改回「继承一个带 title 的基类」的写法时，`title_cn`
    会成为一个真实成员，`list(FileRole)` 里凭空多一项。真实数据恰好干净时
    是看不出来的——所以这里直接断言成员名单。
    """
    from enum import Enum

    for enum_cls in _defined_classes(Enum):
        members = [m.name for m in enum_cls]
        assert "title_cn" not in members, (
            f"{enum_cls.__name__} 的 title_cn 变成了枚举成员：{members}"
        )
