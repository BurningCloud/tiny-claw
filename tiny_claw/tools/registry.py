"""工具注册表与执行层。

对应专栏第 2 章「极简工具与物理交互」与第 16 讲「防御纵深」。

这一层的核心取舍是：只暴露极少数原语（read / write / edit / bash），
而不是为每个业务动作定义一个工具。理由很直接——工具描述本身要占用
上下文窗口，塞给模型几十个工具描述，等于亲手把模型的注意力稀释掉。
给模型一个 bash，它能自己跑 git、grep、make，工具层面的上下文膨胀
从源头就被消灭了。

Registry 负责：注册、路由、按序并发执行，以及承载钩子链（危险命令拦截、
人工审批、耗时统计、Tracing 等横切逻辑）。
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable

from tiny_claw.schema.message import ToolCall, ToolSpec


class Tool(ABC):
    """所有工具的统一接口。

    注意 execute 的入参是原始 JSON 字符串而不是 dict：保持"模型给什么就
    原样传什么"，方便 Tracing 与 Benchmark 复现问题。name 必须与喂给模型的
    ToolSpec.name 完全一致，Registry 靠它路由。
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def input_schema(self) -> str: ...

    @abstractmethod
    async def execute(self, call_input: str) -> str: ...


# 钩子：不调用 next 就等于短路（拦截）。
# 与 Go 的 Hook 不同——这里删掉了 context.Context（取消靠 CancelledError）。
# next 是链条上的下一环，接受"入参字符串"、返回"输出字符串"。
Hook = Callable[[Tool, str, Callable[[str], Awaitable[str]]], Awaitable[str]]


@dataclass
class Execution:
    """一次工具执行的完整记录，交给引擎与 Tracing 消费。"""

    tool_call_id: str
    name: str
    output: str = ""
    err: "BaseException | None" = None
    # blocked 表示被中间件拦截（未真正执行）。
    blocked: bool = False
    duration_ms: int = 0

    def is_error(self) -> bool:
        """判断这次执行是否应当以"失败"的姿态回喂给模型。"""
        return self.err is not None or self.blocked

    def result(self) -> str:
        """生成回喂模型的文本内容。"""
        if self.blocked:
            return self.output
        if self.err is not None:
            return f"tool {self.name} failed: {self.err}"
        if self.output == "":
            return "(no output)"
        return self.output


class Registry:
    """工具注册表：负责注册、路由与并发执行。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._hooks: list[Hook] = []

    def register(self, tool: Tool) -> None:
        """注册一个工具，同名会覆盖（便于测试时替换实现）。"""
        self._tools[tool.name] = tool

    def use(self, hook: Hook) -> None:
        """追加一个执行钩子。先注册的在最外层。"""
        self._hooks.append(hook)

    def get(self, name: str) -> "Tool | None":
        """按名字查找工具。"""
        return self._tools.get(name)

    def all(self) -> "dict[str, Tool]":
        """返回工具快照。

        主要用途：构建子 Agent 的工具集时需要排除 spawn_subagent 本身，
        否则子 Agent 又能开子 Agent，形成无限递归。
        """
        return dict(self._tools)

    def specs(self) -> "list[ToolSpec]":
        """返回喂给模型的工具声明列表，按名字排序以保证请求稳定。

        顺序不稳定会导致 prompt cache 命中率下降，也影响 Benchmark 可比性。
        """
        out = [
            ToolSpec(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
            )
            for t in self._tools.values()
        ]
        out.sort(key=lambda s: s.name)
        return out

    async def execute(self, call: ToolCall) -> Execution:
        """串行执行单次工具调用，走完整钩子链。"""

        async def _fail(err: BaseException) -> Execution:
            return Execution(tool_call_id=call.id, name=call.name, err=err)

        tool = self.get(call.name)
        if tool is None:
            return await _fail(ValueError(f'unknown tool {call.name!r}'))

        hooks = list(self._hooks)

        # 从后往前包，让先注册的钩子在最外层（对应 Go 的闭包包裹顺序）。
        async def base(inp: str) -> str:
            return await tool.execute(inp)

        nxt: Callable[[str], Awaitable[str]] = base
        for h in reversed(hooks):
            inner = nxt
            nxt = lambda inp, _h=h, _inner=inner: _h(tool, inp, _inner)

        start = time.time()
        exec = Execution(tool_call_id=call.id, name=call.name)
        try:
            exec.output = await nxt(call.input)
        except BaseException as exc:  # noqa: BLE001 — 失败也要继续，不能炸掉引擎
            # 拦截型钩子（如 Guard）通过抛 BlockedError 表达"已拦截"，
            # 我们据此把 blocked 置位并把拦截说明写进 output。用类名而非
            # 直接 import，避免 tools 包反向依赖 stability 包（会成环）。
            if type(exc).__name__ == "BlockedError":
                exec.blocked = True
                exec.output = str(exc)
            else:
                exec.err = exc
                out = getattr(exc, "output", None)
                if out:
                    exec.output = out
        exec.duration_ms = int((time.time() - start) * 1000)
        return exec

    async def execute_all(self, calls: "list[ToolCall]") -> "list[Execution]":
        """并发执行一轮里的多个工具调用。

        对应专栏第 8 讲：模型在一轮里给出 N 个互相独立的 tool_call 时，
        串行执行会把端到端延迟放大 N 倍。这里用 asyncio.gather 并行，
        但**结果严格按调用顺序回填**——消息顺序错乱会让模型无法把
        结果和调用对应起来，也会触发部分厂商 API 的 400。
        """
        # gather 本身按传入顺序回填结果，因此并发与"按序"天然一致。
        return await asyncio.gather(*(self.execute(c) for c in calls))
