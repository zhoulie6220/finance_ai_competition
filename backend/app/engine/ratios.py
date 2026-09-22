"""比率与增长率。纯函数、`Decimal`、零 IO、零 LLM。

这是 `engine/` 的第一块比率计算。写它的时候刻意把**拒绝路径**放在与计算路径
同等的位置：`refused` 不是异常处理，是返回值的一种。

会计上最容易出错的地方不是算错，是**算了不该算的**：

    跨年算增长率    2024 对 2022 的增长率，看起来是个数，其实没有意义
    负基期          从 −100 涨到 +50 是「增长 150%」还是「扭亏」？两种都不是
    基期为零        除零，要么抛异常要么变成 inf
    口径不一致      合并口径对母公司口径，两个数根本不在说同一件事

上面每一种都能算出一个数字，而且**都不会报错**。所以本模块的每个函数签名里
都带着「宁可拒绝」的出口——`refused` 非空时 `value` 必然是 `None`，
调用方拿不到一个「带警告的数字」可用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation

#: 口径。与 `app/schemas/enums.py::Scope` 同源，这里用字符串是为了让引擎
#: 不依赖 schemas 包——引擎是纯计算，不该被数据契约的形状牵着走。
CONSOLIDATED = "consolidated"
PARENT = "parent"


@dataclass(frozen=True)
class RatioResult:
    """一个比率 / 增长率的结果。

    `value` 与 `refused` **互斥**：拒绝时 `value` 一定是 None。
    不这么设计的话，一定会有人拿 `value` 直接用，把「不可比」当成「等于 0」——
    这正是本项目立的那条「缺失即无行」在计算层的对应物。
    """

    value: Decimal | None
    formula: str
    inputs: dict[str, str] = field(default_factory=dict)
    refused: str | None = None

    @property
    def ok(self) -> bool:
        return self.refused is None


def _refuse(reason: str, formula: str, **inputs: str) -> RatioResult:
    return RatioResult(value=None, formula=formula, inputs=inputs, refused=reason)


def period_gap(prev_period: str, curr_period: str) -> int | None:
    """两个期间之间隔了几年。解析不了就返回 None（**不猜**）。"""
    try:
        return int(curr_period) - int(prev_period)
    except (TypeError, ValueError):
        return None


def yoy_growth(
    prev: Decimal | None,
    curr: Decimal | None,
    *,
    prev_period: str,
    curr_period: str,
    prev_scope: str = CONSOLIDATED,
    curr_scope: str = CONSOLIDATED,
) -> RatioResult:
    """同比增长率 = (本期 − 上期) / |上期|。

    四种拒绝情形见模块开头。**每一种都对应一个真会发生的场景**，
    不是防御性编程——年报里「上期」栏取不到值、重述导致口径变化、
    2022 年报里对比 2020 年，全都会走到这些分支。
    """
    formula = f"({curr_period} − {prev_period}) / |{prev_period}|"
    # ⚠ 键是**给用户看的**（证据面板里的「代入公式的数」），所以用中文，
    #   而且把年份写进键里——`prev` / `curr` 这种键在界面上完全看不出
    #   哪一年是哪一年，公式 `(2024 − 2023) / |2023|` 就对不上号了。
    inputs = {
        f"{prev_period} 年（上期）": str(prev),
        f"{curr_period} 年（本期）": str(curr),
    }

    if prev is None or curr is None:
        return _refuse("缺值：上期或本期没有数据，不插补", formula, **inputs)
    if prev_scope != curr_scope:
        return _refuse(f"口径不一致：{prev_scope} 对 {curr_scope}", formula, **inputs)

    gap = period_gap(prev_period, curr_period)
    if gap is None:
        return _refuse("期间无法解析为年份", formula, **inputs)
    if gap != 1:
        # ⚠ 这是最隐蔽的一种：2022 对 2020 算出来的是两年的累计变化，
        #   但它在页面上和同比长得一模一样，会平白多出一截或塌掉一截。
        return _refuse(f"期间不连续：隔了 {gap} 年（应为 1 年）", formula, **inputs)

    if prev == 0:
        return _refuse("基期为 0，增长率无定义", formula, **inputs)
    if prev < 0:
        # 负基期下 (c − p)/|p| 会给出反直觉的结果：−100 → +50 算出来是 −150%。
        # 那种「扭亏」不能叫「下降 150%」，但页面上它就是一个负的增长率。
        return _refuse(f"基期为负（{prev}），应看扭亏而非增长率", formula, **inputs)

    try:
        value = ((curr - prev) / abs(prev)).quantize(Decimal("0.0001"))
    except (DivisionByZero, InvalidOperation) as exc:  # pragma: no cover —— 上面已挡
        return _refuse(f"计算失败：{exc}", formula, **inputs)

    return RatioResult(value=value, formula=formula, inputs=inputs)


def ratio(
    numerator: Decimal | None,
    denominator: Decimal | None,
    *,
    name: str,
    scale: Decimal = Decimal("1"),
    labels: tuple[str, str] = ("分子", "分母"),
) -> RatioResult:
    """通用比率。`scale` 用来做百分数（传 `Decimal('100')`）或倍数。

    ⚠ `inputs` 的键是**给用户看的**，必须用中文科目名（`labels`），
    不能写成 `numerator` / `denominator`。它会一路流到界面的证据面板上，
    显示成「代入公式的数：numerator = 17569.485347」——
    评审看到的就是这句英文，而**没有任何地方会报错**。
    键名同时要和 `formula` 里的措辞对上，否则用户没法把两者对起来。
    """
    formula = f"{name} = {labels[0]} / {labels[1]}" + (
        "" if scale == 1 else f" × {scale}"
    )
    inputs = {labels[0]: str(numerator), labels[1]: str(denominator)}

    if numerator is None or denominator is None:
        return _refuse("缺值：分子或分母没有数据，不插补为 0", formula, **inputs)
    if denominator == 0:
        return _refuse("分母为 0，比率无定义", formula, **inputs)

    value = ((numerator / denominator) * scale).quantize(Decimal("0.0001"))
    return RatioResult(value=value, formula=formula, inputs=inputs)


def gross_margin(
    gross_profit: Decimal | None, revenue: Decimal | None
) -> RatioResult:
    """毛利率。营业收入为 0 或缺失时拒绝，不返回 0。"""
    if revenue is not None and revenue < 0:
        return _refuse(f"营业收入为负（{revenue}），毛利无意义", "毛利 / 营业收入")
    return ratio(
        gross_profit, revenue,
        name="毛利率", scale=Decimal("100"), labels=("毛利", "营业收入"),
    )
