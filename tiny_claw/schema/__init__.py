"""schema：与模型厂商无关的中立消息表示。

Harness 内部只有一种消息结构。各家厂商的格式差异（Anthropic 的 content block、
OpenAI 的 tool_calls 数组）全部由 provider 层负责翻译，引擎与上下文层
只见得到这里定义的东西。换一个 Provider 不需要动引擎一行代码。

公开名字遵循 CONTRACT.md 第 1 节。
"""

from __future__ import annotations

from tiny_claw.schema.message import (
    STOP_END,
    STOP_MAX_TOK,
    STOP_TOOL_USE,
    Message,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
    new_assistant,
    new_tool_result,
    new_user,
)

__all__ = [
    "Role",
    "Message",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "new_user",
    "new_assistant",
    "new_tool_result",
    "STOP_TOOL_USE",
    "STOP_END",
    "STOP_MAX_TOK",
]
