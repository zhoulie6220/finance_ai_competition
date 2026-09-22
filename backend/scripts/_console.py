"""控制台输出的编码兜底。

Windows 上 Python 的 stdout 默认跟随控制台代码页（简体中文机器上是 GBK/cp936），
而本项目的所有提示语都是中文、还带 ✓ / ✗ 这类符号。于是 `print("✓ 已写入")`
会抛 UnicodeEncodeError —— 而且是在**全部工作做完之后**才抛：

    ✓ 已写入 89 个字段 ...

变成

    Traceback (most recent call last):
    UnicodeEncodeError: 'gbk' codec can't encode character '✓'

脚本实际上已经成功了，用户看到的却是一个 traceback，会以为写失败了并重跑一遍。
会计同学改字典用的正是 `dict_csv.py --import`，这条路径不能这个样子。

所以每个面向人的脚本都在最前面调一次 `setup()`。
"""

from __future__ import annotations

import sys


def setup() -> None:
    """把 stdout / stderr 切到 UTF-8，转不动的字符降级为替代符而不是抛异常。

    `errors="replace"` 是刻意的：日志里出现一个 `?` 远好过整个脚本崩掉。
    真正需要精确还原的内容（种子数据、CSV、数据库）全部走文件 IO，
    那些地方一律显式指定 encoding='utf-8'，不依赖这个兜底。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
