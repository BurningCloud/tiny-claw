"""消息与工具的中立表示层。

设计要点（对应专栏第 1 章「认知与核心引擎」、第 3 章「上下文工程体系」）：

1. 各家厂商的消息格式千差万别（Anthropic 用 content block，OpenAI 用
   tool_calls 数组）。Harness 必须有一层"中立表示"，否则换一个 Provider
   就要把整个引擎重写一遍。

2. Thinking 是独立于 Content 的字段。"慢思考"不是"让模型想久一点"，
   而是 ReAct 循环里一个独立的阶段。独立的字段意味着它可以被单独
   丢弃、单独压缩、单独送入 Tracing —— 这是压缩策略能生效的前提。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    """消息角色。

    注意 SYSTEM 只在组装请求时存在，不进入会话历史
    （系统提示由 Composer 每轮动态重建）。

    继承 str 是为了让 json.dumps 直接输出其字面值，
    避免到处写 .value。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str
    name: str
    # Input 保留原始 JSON 字符串，不在中间层反复 marshal/unmarshal，
    # 既省 CPU 也避免字段被静默改写。
    input: str = ""

    def decode(self) -> dict[str, Any]:
        """解析入参。空串按空对象处理，避免工具侧到处判空。"""
        raw = self.input or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"tool_call input is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("tool_call input must be a JSON object")
        return data


@dataclass
class ToolSpec:
    """喂给模型的工具声明，与具体 Provider 的 wire format 无关。"""

    name: str
    description: str
    # InputSchema 是 JSON Schema 文本（字符串，不是 dict），
    # 这样 Provider 可以原样拼进请求体，不做二次编码。
    input_schema: str = ""


@dataclass
class Usage:
    """单次请求的 token 用量。成本审计与上下文水位判断都依赖它。"""

    input_tokens: int = 0
    output_tokens: int = 0

    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Message:
    """引擎内部流转的唯一消息结构。"""

    role: Role
    content: str = ""
    # Thinking 存放模型的慢思考产物，与正式回复 Content 分离。
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""
    is_error: bool = False
    created_at: float = field(default_factory=time.time)
    # Pinned 为 True 的消息不会被上下文压缩器丢弃（如用户的原始任务描述）。
    pinned: bool = False


def new_user(text: str) -> Message:
    """构造用户消息。用户的原始指令默认 Pin，压缩时永不丢失。"""
    return Message(role=Role.USER, content=text, pinned=True)


def new_assistant(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def new_tool_result(call_id: str, name: str, content: str, is_error: bool) -> Message:
    return Message(
        role=Role.TOOL,
        content=content,
        name=name,
        tool_call_id=call_id,
        is_error=is_error,
    )


# 模型停止原因。引擎据此决定是继续循环还是收工。
STOP_TOOL_USE = "tool_use"
STOP_END = "end_turn"
STOP_MAX_TOK = "max_tokens"
