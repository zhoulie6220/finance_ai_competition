"""共享标量类型。

金额一律用 Decimal，并在 JSON 序列化时**强制转成字符串**。原因：JSON 的 number
是 IEEE 754 双精度，12,345,678,901.23 这种金额在往返一次之后就可能变成
12345678901.229998。财务数据不允许这种误差，所以出网即字符串，由前端按展示
需要格式化。入网时 Pydantic 会把字符串解析回 Decimal。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer, PlainValidator


def _to_decimal(v: object) -> Decimal:
    """把 str/int/float 统一转成 Decimal。

    走 str 中转而不是 Decimal(float)：Decimal(0.1) 会得到
    0.1000000000000000055511151231257827021181583404541015625，
    而 Decimal('0.1') 才是财务上期望的 0.1。
    """
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        return Decimal(str(v))
    return Decimal(v)  # type: ignore[arg-type]


DecimalInput = Annotated[Decimal, PlainValidator(_to_decimal)]

# 货币金额。单位统一为百万元，原始单位与换算因子另存。
Money = Annotated[DecimalInput, PlainSerializer(lambda v: str(v), return_type=str, when_used="json")]

# 比率。0.0742 表示 7.42%，不做百分数转换以免两处口径打架。
Ratio = Annotated[DecimalInput, PlainSerializer(lambda v: str(v), return_type=str, when_used="json")]
