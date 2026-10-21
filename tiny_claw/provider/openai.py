"""OpenAI（Chat Completions 及兼容实现）Provider。

对应 Go 版的 openai.go。适配 OpenAI Chat Completions 及其兼容实现
（DeepSeek、通义、Kimi、vLLM、Ollama 等大多走这套协议）。

与 Anthropic 不同：system 是数组里的第一条消息，工具回执是独立的 tool
角色。零依赖：HTTP 用标准库 urllib.request，阻塞 IO 包在 asyncio.to_thread 里。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from tiny_claw.provider.base import (
    ChatRequest,
    ChatResponse,
    EmptyResponseError,
    Provider,
)
from tiny_claw.provider.http import do_post, or_empty_object
from tiny_claw.schema.message import (
    Message,
    Role,
    STOP_TOOL_USE,
    STOP_END,
    STOP_MAX_TOK,
    ToolSpec,
    ToolCall,
    Usage,
)

DEFAULT_TIMEOUT_MS = 600_000


@dataclass
class OpenAIConfig:
    """OpenAI Chat Completions 接入配置（及兼容端点通用）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    # ReasoningEffort 非空时，开启慢思考会带上 reasoning_effort 参数。
    # 留空则表示该端点不支持（多数兼容实现确实不支持，硬传会 400）。
    reasoning_effort: str = ""
    # UseMaxCompletionTokens 用于新版模型（o 系列及之后）要求
    # max_completion_tokens 而非 max_tokens 的场景。
    use_max_completion_tokens: bool = False


class OpenAI(Provider):
    """适配 OpenAI Chat Completions 的 Provider（Go 版叫 OpenAIProvider）。"""

    def __init__(self, cfg: OpenAIConfig) -> None:
        if cfg.base_url == "":
            cfg.base_url = "https://ws-mfhzl0o4y4pujxjq.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        if cfg.model == "":
            cfg.model = "qwen3.8-flash"
        if cfg.timeout_ms <= 0:
            cfg.timeout_ms = DEFAULT_TIMEOUT_MS
        self._cfg = cfg

    @property
    def name(self) -> str:
        return "openai"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        if req is None:
            raise ValueError("provider(openai): nil request")
        if len(req.messages) == 0 and req.system == "":
            raise ValueError("provider(openai): empty conversation")

        max_tokens = req.max_tokens
        if max_tokens <= 0:
            max_tokens = 8192

        body: dict[str, object] = {
            "model": self._cfg.model,
            "messages": to_openai_messages(req.system, req.messages),
            "max_tokens": max_tokens,
        }
        if self._cfg.use_max_completion_tokens:
            # 新版模型用 max_completion_tokens 取代 max_tokens。
            del body["max_tokens"]
            body["max_completion_tokens"] = max_tokens
        if req.temperature > 0:
            body["temperature"] = req.temperature
        if len(req.tools) > 0:
            tools = []
            for t in req.tools:
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": json.loads(or_empty_object(t.input_schema)),
                        },
                    }
                )
            body["tools"] = tools
        if req.thinking and self._cfg.reasoning_effort != "":
            body["reasoning_effort"] = self._cfg.reasoning_effort

        headers: dict[str, str] = {}
        if self._cfg.api_key != "":
            headers["Authorization"] = "Bearer " + self._cfg.api_key

        start = time.time()
        data = await asyncio.to_thread(
            do_post,
            self._cfg.base_url + "/chat/completions",
            headers,
            body,
            timeout_ms=self._cfg.timeout_ms,
        )

        out = _OpenAIResponse.model_validate(data)

        # 没有候选 → 视为空响应（Go 版返回 (nil, ErrEmptyResponse)）。
        if len(out.choices) == 0:
            raise EmptyResponseError()

        msg = out.choices[0].message
        resp = ChatResponse(
            content=msg.content,
            stop_reason=map_openai_stop(out.choices[0].finish_reason),
            usage=Usage(
                input_tokens=out.usage.prompt_tokens,
                output_tokens=out.usage.completion_tokens,
            ),
            latency_ms=int((time.time() - start) * 1000),
            model=out.model or self._cfg.model,
        )
        # 不同兼容实现对思考内容的字段名不统一，两个都兜一下。
        if msg.reasoning != "":
            resp.thinking = msg.reasoning
        elif msg.reasoning2 != "":
            resp.thinking = msg.reasoning2

        for tc in msg.tool_calls:
            resp.tool_calls.append(
                ToolCall(id=tc.id, name=tc.function.name, input=tc.function.arguments)
            )

        if resp.content == "" and len(resp.tool_calls) == 0 and resp.thinking == "":
            raise EmptyResponseError()
        return resp


def to_openai_messages(sys: str, msgs: list[Message]) -> list[dict[str, object]]:
    """把中立消息结构翻译成 Chat Completions 的消息数组。

    与 Anthropic 不同：system 是数组里的第一条消息，工具回执是独立的 tool 角色。
    """
    out: list[dict[str, object]] = []
    if sys != "":
        out.append({"role": "system", "content": sys})
    for m in msgs:
        if m.role == Role.SYSTEM:
            # 历史里若夹带了 system 角色（理论上不应有），有内容就追加成 system 消息。
            if m.content != "":
                out.append({"role": "system", "content": m.content})
        elif m.role == Role.USER:
            out.append({"role": "user", "content": m.content})
        elif m.role == Role.TOOL:
            tool_msg: dict[str, object] = {
                "role": "tool",
                "tool_call_id": m.tool_call_id,
                "content": m.content,
            }
            if m.name != "":
                tool_msg["name"] = m.name
            out.append(tool_msg)
        elif m.role == Role.ASSISTANT:
            msg: dict[str, object] = {"role": "assistant", "content": m.content}
            if m.thinking != "":
                msg["reasoning_content"] = m.thinking
            if len(m.tool_calls) > 0:
                tcs = []
                for tc in m.tool_calls:
                    tcs.append(
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": or_empty_object(tc.input),
                            },
                        }
                    )
                msg["tool_calls"] = tcs
                if m.content == "":
                    # 纯工具调用轮次，content 传 null 更省 token。
                    msg["content"] = None
            out.append(msg)
    return out


def map_openai_stop(reason: str) -> str:
    """OpenAI finish_reason → 中立 STOP_* 常量。"""
    if reason in ("tool_calls", "function_call"):
        return STOP_TOOL_USE
    elif reason == "length":
        return STOP_MAX_TOK
    return STOP_END


class _OpenAIResponse:
    model: str = ""
    choices: list["_Choice"]
    usage: "_Usage"

    @classmethod
    def model_validate(cls, data: bytes) -> "_OpenAIResponse":
        raw = json.loads(data)
        obj = cls()
        obj.model = raw.get("model", "")
        choices: list[_Choice] = []
        for c in raw.get("choices", []) or []:
            message = c.get("message", {}) or {}
            funcs: list[_ToolCall] = []
            for tc in message.get("tool_calls", []) or []:
                func = tc.get("function", {}) or {}
                funcs.append(
                    _ToolCall(
                        id=tc.get("id", ""),
                        function=_Function(
                            name=func.get("name", ""),
                            # OpenAI 的 arguments 本来就是 JSON 文本字符串，原样保留。
                            arguments=func.get("arguments", ""),
                        ),
                    )
                )
            choices.append(
                _Choice(
                    finish_reason=c.get("finish_reason", ""),
                    message=_Message(
                        content=message.get("content", "") or "",
                        reasoning=message.get("reasoning_content", "") or "",
                        reasoning2=message.get("reasoning", "") or "",
                        tool_calls=funcs,
                    ),
                )
            )
        obj.choices = choices
        usage = raw.get("usage", {}) or {}
        obj.usage = _Usage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
        return obj


class _Usage:
    def __init__(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Choice:
    def __init__(self, finish_reason: str = "", message: "_Message | None" = None) -> None:
        self.finish_reason = finish_reason
        self.message = message


class _Message:
    def __init__(
        self,
        content: str = "",
        reasoning: str = "",
        reasoning2: str = "",
        tool_calls: list["_ToolCall"] | None = None,
    ) -> None:
        self.content = content
        self.reasoning = reasoning
        self.reasoning2 = reasoning2
        self.tool_calls = tool_calls or []


class _ToolCall:
    def __init__(self, id: str = "", function: "_Function | None" = None) -> None:
        self.id = id
        self.function = function


class _Function:
    def __init__(self, name: str = "", arguments: str = "") -> None:
        self.name = name
        self.arguments = arguments


__all__ = ["OpenAIConfig", "OpenAI", "to_openai_messages", "map_openai_stop"]
