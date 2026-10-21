"""Provider 层：大脑接入层。

把 Harness 核心资产（循环、工具、上下文、稳定性）与模型厂商的 API 变更
彻底解耦。这一层不"多支持一个厂商"，而是让引擎不再依赖任何具体模型。

公开名字（供 bootstrap.new_provider、engine 等跨层调用）：
  - 接口契约：ChatRequest / ChatResponse / Provider
  - 异常：EmptyResponseError / ContextTooLongError
  - 实现：Claude / ClaudeConfig、OpenAI / OpenAIConfig
"""

from tiny_claw.provider.base import (
    ChatRequest,
    ChatResponse,
    Provider,
    EmptyResponseError,
    ContextTooLongError,
)
from tiny_claw.provider.claude import Claude, ClaudeConfig
from tiny_claw.provider.openai import OpenAI, OpenAIConfig

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "Provider",
    "EmptyResponseError",
    "ContextTooLongError",
    "Claude",
    "ClaudeConfig",
    "OpenAI",
    "OpenAIConfig",
]
