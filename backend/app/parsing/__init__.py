"""PDF 解析：文本与表格抽取、报表定位、行名映射。

**这一层不认识数据库、不做业务判断。** 它只把 PDF 变成结构化的报表行，
落库与口径判定在上层。分开的理由是解析可以被单独测试：
喂一份 PDF 进去、断言出来的行，不需要建库。

     reader.py      PDF → 逻辑行（按坐标切列、按行距合并折行）
     statements.py  定位三张主表、解表头、按列读行
     mapping.py     行名 → metric_key（走字段字典，精确匹配）

⚠ 解析结果**一律要带页码与原文**。这个系统的卖点是「点任意结论回到年报原文」，
  解析时如果只留下数字，后面无论怎么补都补不回出处。
"""

from app.parsing.models import (
    BALANCE,
    CASHFLOW,
    INCOME,
    UNIT_FACTORS,
    ParseReport,
    ParsedRow,
    ParsedStatement,
)
from app.parsing.statements import parse_document, parse_number

__all__ = [
    "BALANCE", "CASHFLOW", "INCOME", "UNIT_FACTORS",
    "ParseReport", "ParsedRow", "ParsedStatement",
    "parse_document", "parse_number",
]
