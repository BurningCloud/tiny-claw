"""Package provider 抽象了"大脑接入层"。

对应专栏第 4 讲：抽象 Provider 接口，适配 Claude 与 OpenAI 兼容大模型。
这一层的存在意义不是"多支持一个厂商"，而是把 Harness 的核心资产
（循环、工具、上下文、稳定性）与模型厂商的 API 变更彻底解耦。

Harness 的核心价值在控制流与编排，而不在某个具体模型上。把"调哪家模型"
收敛成一个统一的 `Provider` 接口之后，引擎（engine）、工具（tools）、
上下文（context）、稳定性（stability）这些真正值钱的核心资产，就不再
会因为 Anthropic 改一次字段、OpenAI 加一个参数而被迫跟着改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from tiny_claw.schema.message import (
    Message,
    ToolCall,
    ToolSpec,
    Usage,
    STOP_TOOL_USE,
    STOP_END,
    STOP_MAX_TOK,
)


@dataclass
class ChatRequest:
    """一次模型调用的入参。

    注意：System 由 Composer 每轮动态组装后传入，不进入会话历史。
    """

    # System 是系统提示，由 Composer 每轮动态组装后传入。
    system: str = ""
    # Messages 是会话历史（不含 system）。
    messages: list[Message] = field(default_factory=list)
    tools: list[ToolSpec] = field(default_factory=list)
    # MaxTokens 是本次回复的 token 上限。
    max_tokens: int = 8192
    # Temperature 是采样温度。启用 Thinking 时部分厂商要求固定为 1，
    # 具体适配在各 Provider 内部处理。
    temperature: float = 0.0
    # Thinking 为 True 时要求模型先产出独立的慢思考内容。
    thinking: bool = False
    # ThinkingBudget 是慢思考的 token 预算，<=0 时由 Provider 取默认值。
    thinking_budget: int = 0


@dataclass
class ChatResponse:
    """一次模型调用的出参。"""

    content: str = ""
    # Thinking 是慢思考阶段的输出，与 Content 分离保存。
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    # StopReason 取值见 schema 的 STOP_* 常量。
    stop_reason: str = ""
    # LatencyMs 用于可观测性与 Benchmark（Go 版是 time.Duration，这里换成毫秒整数）。
    latency_ms: int = 0
    # Model 是实际命中的模型名（成本审计需要真实模型，而非逻辑名）。
    model: str = ""


class EmptyResponseError(Exception):
    """模型返回了空内容且没有工具调用。

    引擎遇到它会注入提醒并重试，而不是直接崩掉——空回复通常是模型
    临时的"卡壳"，而非真正的失败。
    """


class ContextTooLongError(Exception):
    """上下文超出模型窗口。

    引擎拿到这个信号会触发紧急压缩，而不是把错误原样丢给用户。
    """


class Provider(ABC):
    """所有大模型接入的统一接口。

    实现约束（与 Go 版一致）：
      - 必须是并发安全的（多个 Session 可能同时调用）；
      - 错误要区分"可重试"与"不可重试"，由实现自行包装；
      - 不得修改入参。

    Go 版的 `Name() string` 与 `Chat(ctx, req) (*ChatResponse, error)`
    在 Python 里做了两处映射：
      - context.Context 直接删掉，取消靠 asyncio.CancelledError；
      - error 返回值改为抛异常（EmptyResponseError / ContextTooLongError
        等可预期业务失败，其它用 RuntimeError 兜底）。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 的逻辑名，如 "claude" / "openai"。"""
        ...

    @abstractmethod
    async def chat(self, req: ChatRequest) -> ChatResponse:
        """发起一轮对话。ctx 取消时底层 HTTP 请求必须同步退出。"""
        ...


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "EmptyResponseError",
    "ContextTooLongError",
    "Provider",
    "STOP_TOOL_USE",
    "STOP_END",
    "STOP_MAX_TOK",
]
