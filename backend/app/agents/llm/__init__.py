"""模型接入层：配置、传输、版本化 Prompt、结构化输出客户端。

分层：
    settings.py   配置（密钥只从这里读，不落库不进日志）
    transport.py  实时 / 录制 / 回放三种传输，接口一致、可注入
    client.py     结构化 JSON + schema_repair 重试
    prompts/      版本化 Prompt 注册表

**边界**：这一层只负责「把文字交给模型、把文字拿回来」。
数字、公式、判定全部由 app/engine/ 的纯函数算——模型只理解与组织文字。
"""
