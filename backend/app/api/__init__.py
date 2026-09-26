"""REST 路由。薄层，只做参数校验与调用编排，不写业务逻辑。

模块划分：
    routes.py  REST 路由（项目、事实、原文、勾稽、规则参数）
    SSE 的路由在 app/main.py —— 它要挂 StreamingResponse，与普通 JSON 路由
    放在一起会让依赖注入的写法分叉，不如分开。
"""

from app.api.routes import get_con, router

__all__ = ["router", "get_con"]
