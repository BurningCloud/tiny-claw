"""Claude（Anthropic Messages API）Provider。

对应 Go 版的 claude.go。与 OpenAI 的关键差异（也是必须抽象掉的部分）：
  - 系统提示是顶层 system 字段，不在 messages 数组里；
  - 消息 content 是"块数组"，文本 / 思考 / 工具调用是不同的块类型；
  - 工具回执必须作为 user 消息里的 tool_result 块跟在 tool_use 之后。

零依赖：HTTP 用标准库 urllib.request，阻塞 IO 包在 asyncio.to_thread 里。
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

# 默认 10 分钟超时，对应 Go 版 http.Client{Timeout: 10 * time.Minute}，
# 也作为 do_post 的 timeout_ms 默认值。
DEFAULT_TIMEOUT_MS = 600_000


@dataclass
class ClaudeConfig:
    """Anthropic Messages API 的接入配置。"""

    # BaseURL 允许指向自建网关或代理，默认官方地址。
    base_url: str = ""
    api_key: str = ""
    # Model 是具体模型标识，建议显式指定而不是依赖默认值。
    model: str = ""
    # ThinkingBudget 是慢思考的默认 token 预算。
    thinking_budget: int = 0
    # TimeoutMs 是单次 HTTP 请求的超时（毫秒）。Go 版用 *http.Client，
    # 这里零依赖改为纯超时参数。
    timeout_ms: int = DEFAULT_TIMEOUT_MS


class Claude(Provider):
    """适配 Anthropic Messages API 的 Provider。

    Go 版结构体叫 ClaudeProvider，构造函数叫 NewClaude；契约规定 Python
    类名用 Claude + ClaudeConfig，构造即 `Claude(cfg)`。
    """

    def __init__(self, cfg: ClaudeConfig) -> None:
        if cfg.base_url == "":
            cfg.base_url = "https://api.anthropic.com"
        if cfg.model == "":
            cfg.model = "claude-sonnet-4-5"
        if cfg.thinking_budget <= 0:
            cfg.thinking_budget = 4096
        if cfg.timeout_ms <= 0:
            cfg.timeout_ms = DEFAULT_TIMEOUT_MS
        self._cfg = cfg

    @property
    def name(self) -> str:
        return "claude"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        if req is None:  # 对应 Go 版的 nil 判空
            raise ValueError("provider(claude): nil request")

        messages = to_claude_messages(req.messages)

        max_tokens = req.max_tokens
        if max_tokens <= 0:
            max_tokens = 8192

        body: dict[str, object] = {
            "model": self._cfg.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if req.system != "":
            body["system"] = req.system
        if len(req.tools) > 0:
            tools = []
            for t in req.tools:
                tools.append(
                    {
                        "name": t.name,
                        "description": t.description,
                        # json.RawMessage(orEmptyObject(...)) 的等价：
                        # 把 JSON Schema 文本解析回对象再塞进 body，
                        # 避免被 json.dumps 二次转义成字符串。
                        "input_schema": json.loads(or_empty_object(t.input_schema)),
                    }
                )
            body["tools"] = tools

        if req.thinking:
            budget = req.thinking_budget
            if budget <= 0:
                budget = self._cfg.thinking_budget
            # 思考预算必须严格小于 max_tokens，否则 API 直接报错。
            if budget >= max_tokens:
                budget = max_tokens // 2
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Anthropic 要求开启 extended thinking 时 temperature 必须为 1。
            body["temperature"] = 1
        elif req.temperature > 0:
            body["temperature"] = req.temperature

        start = time.time()
        data = await asyncio.to_thread(
            do_post,
            self._cfg.base_url + "/v1/messages",
            {
                "x-api-key": self._cfg.api_key,
                "anthropic-version": "2023-06-01",
            },
            body,
            timeout_ms=self._cfg.timeout_ms,
        )

        out = _ClaudeResponse.model_validate(data)
        resp = ChatResponse(
            stop_reason=map_claude_stop(out.stop_reason),
            usage=Usage(
                input_tokens=out.usage.input_tokens,
                output_tokens=out.usage.output_tokens,
            ),
            # Latency：Go 版 time.Since(start) 是 Duration，这里换成毫秒整数。
            latency_ms=int((time.time() - start) * 1000),
            model=out.model or self._cfg.model,
        )

        for block in out.content:
            if block.type == "thinking":
                resp.thinking += block.thinking
            elif block.type == "text":
                resp.content += ("\n" if resp.content else "") + block.text
            elif block.type == "tool_use":
                resp.tool_calls.append(
                    ToolCall(id=block.id, name=block.name, input=block.input)
                )

        # 空内容 + 无工具调用 + 无思考：Go 版返回 (resp, ErrEmptyResponse)。
        # 这里按契约"可预期失败抛异常"的约定，直接抛 EmptyResponseError；
        # 引擎只依赖这个错误信号，部分填充的 resp 没有额外信息可保留。
        if resp.content == "" and len(resp.tool_calls) == 0 and resp.thinking == "":
            raise EmptyResponseError()
        return resp


def to_claude_messages(msgs: list[Message]) -> list[dict[str, object]]:
    """把中立消息结构翻译成 Anthropic 的 content-block 形式。

    Anthropic 要求 tool_result 紧跟在对应的 tool_use 之后。连续多条工具回执
    合并进同一条 user 消息，避免出现空的 assistant 轮次。
    """
    out: list[dict[str, object]] = []

    # 累积中的 tool_result 块，遇到非 tool 角色的消息就 flush 成一条 user 消息。
    pending: list[dict[str, object]] = []

    def flush() -> None:
        if len(pending) > 0:
            out.append({"role": "user", "content": list(pending)})
            pending.clear()

    for m in msgs:
        if m.role == Role.SYSTEM:
            continue  # 系统提示走顶层 system 字段
        elif m.role == Role.TOOL:
            block: dict[str, object] = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                # Anthropic 不接受空字符串的 tool_result content。
                "content": or_empty(m.content),
            }
            if m.is_error:
                block["is_error"] = True
            pending.append(block)
        elif m.role == Role.USER:
            flush()
            out.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": m.content}],
                }
            )
        elif m.role == Role.ASSISTANT:
            flush()
            blocks: list[dict[str, object]] = []
            if m.thinking != "":
                blocks.append({"type": "thinking", "thinking": m.thinking})
            if m.content != "":
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": json.loads(or_empty_object(tc.input)),
                    }
                )
            if len(blocks) == 0:
                # assistant 消息不能是空 content，否则 API 400。
                blocks.append({"type": "text", "text": ""})
            out.append({"role": "assistant", "content": blocks})

    flush()

    if len(out) == 0:
        raise ValueError("provider(claude): no messages to send")
    # Anthropic 要求 messages 首条必须是 user。
    if out[0]["role"] != "user":
        out.insert(
            0,
            {
                "role": "user",
                "content": [{"type": "text", "text": "(continue)"}],
            },
        )
    return out


def map_claude_stop(reason: str) -> str:
    """Anthropic stop_reason → 中立 STOP_* 常量。"""
    if reason == "tool_use":
        return STOP_TOOL_USE
    elif reason == "max_tokens":
        return STOP_MAX_TOK
    return STOP_END


def or_empty(value: str) -> str:
    """Anthropic 不接受空字符串的 tool_result content。"""
    if value == "":
        return "(empty)"
    return value


class _ClaudeResponse:
    """把原始 JSON 响应解析成好用的结构（手写轻量解析，避免引入 pydantic）。"""

    model: str = ""
    stop_reason: str = ""
    usage: "_Usage"
    content: list["_Block"]

    @classmethod
    def model_validate(cls, data: bytes) -> "_ClaudeResponse":
        raw = json.loads(data)
        obj = cls()
        obj.model = raw.get("model", "")
        obj.stop_reason = raw.get("stop_reason", "")
        usage = raw.get("usage", {}) or {}
        obj.usage = _Usage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
        blocks: list[_Block] = []
        for b in raw.get("content", []) or []:
            blocks.append(
                _Block(
                    type=b.get("type", ""),
                    text=b.get("text", ""),
                    thinking=b.get("thinking", ""),
                    id=b.get("id", ""),
                    name=b.get("name", ""),
                    # 工具入参原样保留为 JSON 文本字符串，与 ToolCall.input 约定一致。
                    input=json.dumps(b.get("input", {}), ensure_ascii=False)
                    if isinstance(b.get("input"), (dict, list))
                    else str(b.get("input", "")),
                )
            )
        obj.content = blocks
        return obj


class _Usage:
    def __init__(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Block:
    def __init__(
        self,
        type: str = "",
        text: str = "",
        thinking: str = "",
        id: str = "",
        name: str = "",
        input: str = "",
    ) -> None:
        self.type = type
        self.text = text
        self.thinking = thinking
        self.id = id
        self.name = name
        self.input = input


__all__ = ["ClaudeConfig", "Claude", "to_claude_messages", "map_claude_stop"]
