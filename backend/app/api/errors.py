"""统一的中文错误响应。

FastAPI / Pydantic 的报错默认是英文（"String should have at least 1 character"），
而这些字符串**会直接显示在页面上**——它们出现在 `detail[0].msg` 里，前端原样渲染。
一份中文界面上突然冒出一句英文，看起来像系统坏了，而不是像「你少填了一个字段」。

这里把 `type`（机读错误码）映射成中文，`type` 本身**原样保留**在响应里：
文案会改，码不会，前端要分支判断就认 `type`。

⚠ 响应结构与 FastAPI 的默认 422 **保持一致**（`{"detail": [...]}`，每项带
  `loc` / `type` / `msg`）。改结构会让所有前端里已经写好的 `detail[0].msg`
  取值方式失效——那是所有人都见过的默认约定，不要动它。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.api import ValidationErrorResponse, ValidationIssue

#: 出现在 loc 首位的「位置」标记，展示时要剥掉——用户不关心是 body 还是 query
_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})

#: 错误码 → 中文模板。模板里的 `{...}` 用 Pydantic 的 `ctx` 填充。
#:
#: 覆盖不到的类型走 `_FALLBACK`，**不会**静默变成空串：一句「取值不合法（错误码：xxx）」
#: 至少还能让人拿着码去查，而空字符串会让人以为是接口坏了。
_TYPE_ZH: dict[str, str] = {
    # 结构
    "missing": "缺少必填字段",
    "json_invalid": "请求体不是合法的 JSON",
    "extra_forbidden": "不认识的字段",
    "dict_type": "应为对象",
    "list_type": "应为数组",
    # 字符串
    "string_type": "应为字符串",
    "string_too_short": "至少需要 {min_length} 个字符",
    "string_too_long": "最多允许 {max_length} 个字符",
    "string_pattern_mismatch": "格式不符合要求",
    "string_unicode": "含有无法编码的字符",
    # 数字
    "int_type": "应为整数",
    "int_parsing": "无法解析为整数",
    "float_type": "应为数字",
    "float_parsing": "无法解析为数字",
    "decimal_type": "应为数字",
    "decimal_parsing": "无法解析为数字",
    "greater_than": "必须大于 {gt}",
    "greater_than_equal": "必须大于等于 {ge}",
    "less_than": "必须小于 {lt}",
    "less_than_equal": "必须小于等于 {le}",
    "multiple_of": "必须是 {multiple_of} 的整数倍",
    # 其它标量
    "bool_type": "应为布尔值",
    "bool_parsing": "无法解析为布尔值",
    "date_type": "应为日期",
    "date_parsing": "无法解析为日期",
    "datetime_type": "应为时间",
    "datetime_parsing": "无法解析为时间",
    "bytes_type": "应为字节串",
    # 取值
    "enum": "取值不在允许范围内",
    "literal_error": "取值不在允许范围内",
    "value_error": "值不合法",
}

_FALLBACK = "取值不合法（错误码：{type}）"


def _fmt(template: str, err: dict[str, Any]) -> str:
    """用 ctx 填充模板。缺哪个键就原样留下占位符——**不要静默填空**。"""
    ctx = err.get("ctx") or {}
    try:
        return template.format(**ctx)
    except (KeyError, IndexError, ValueError):
        return template


def field_path(loc: tuple[Any, ...] | list[Any]) -> str:
    """把 Pydantic 的 loc 串变成 `project_id` / `steps.0.name` 这样的路径。"""
    parts = [p for p in loc if p not in _LOCATIONS]
    return ".".join(str(p) for p in parts) or "(请求体)"


def _jsonable(value: Any) -> Any:
    """把 ctx / input 里塞不进 JSON 的对象转成字符串。

    `value_error` 的 ctx 里装的是原始的 `ValueError` 对象，直接交给 JSONResponse
    会抛 `TypeError: Object of type ValueError is not JSON serializable`——
    于是「参数校验失败」变成「500 服务器内部错误」，错误处理自己成了错误源。
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def to_issue(err: dict[str, Any]) -> ValidationIssue:
    template = _TYPE_ZH.get(err["type"], _FALLBACK)
    return ValidationIssue(
        field=field_path(err.get("loc", ())),
        loc=[str(p) if not isinstance(p, int) else p for p in err.get("loc", ())],
        type=err["type"],
        msg=_fmt(template, err),
        input=_jsonable(err.get("input")),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """把 422 的报错文案换成中文。形状不变。"""
    issues = [to_issue(e) for e in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"detail": [i.model_dump() for i in issues]},
    )


#: 挂在每个 APIRouter 上的响应说明。
#:
#: 不挂的话，`/docs` 上每个接口下面写的是 "Successful Response" 和
#: "Validation Error"——FastAPI 的英文默认值，改不了措辞只能覆盖。
#:
#: ⚠ 只声明 200 和 422 这一对**所有接口都成立**的。400 / 404 / 409 是具体接口才有的，
#:   挂到每个接口上会让文档显示一堆这些接口根本不会返回的码——错误的文档
#:   比没有文档更糟，因为它看起来是对的。
COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "成功"},
    422: {"model": ValidationErrorResponse, "description": "请求参数未通过校验"},
}
