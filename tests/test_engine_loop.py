"""引擎主循环端到端测试（移植自 Go 版 internal/engine/loop_test.go）。

用桩 Provider 在不联网的情况下验证：工具调用 → 回执回填 → 最终回答，
以及轮次上限兜底、空输入拒绝。

一个容易踩的坑：Engine 的默认 Composer 会在构造时读工作目录下的
AGENTS.md / 技能目录，因此这里显式传一个空配置的 Composer，
保证测试与磁盘状态无关（可重复）。
"""

from __future__ import annotations

import unittest

from tiny_claw.context import Composer, Session
from tiny_claw.engine import Config, Engine, EngineError
from tiny_claw.provider import ChatRequest, ChatResponse, Provider
from tiny_claw.schema import STOP_END, STOP_TOOL_USE, Role, ToolCall, Usage
from tiny_claw.tools import Registry, Tool


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class ScriptedProvider(Provider):
    """按脚本返回回复，用来在不联网的情况下验证主循环。"""

    def __init__(self, replies: list[ChatResponse] | None = None) -> None:
        self.replies: list[ChatResponse] = replies or []
        self.i = 0
        # requests 记录收到的请求，便于断言上下文组装是否正确。
        self.requests: list[ChatRequest] = []

    @property
    def name(self) -> str:
        return "fake"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        self.requests.append(req)
        if self.i >= len(self.replies):
            # 脚本用尽后一直返回工具调用，用来测试轮次上限。
            return ChatResponse(
                tool_calls=[ToolCall(id="loop", name="echo", input='{"v":"again"}')],
                stop_reason=STOP_TOOL_USE,
            )
        r = self.replies[self.i]
        self.i += 1
        return r


class EchoTool(Tool):
    """回显工具。"""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回显入参"

    @property
    def input_schema(self) -> str:
        return '{"type":"object","properties":{"v":{"type":"string"}}}'

    async def execute(self, call_input: str) -> str:
        return "echo:" + call_input


def _new_test_engine(p: Provider, max_turns: int) -> Engine:
    reg = Registry()
    reg.register(EchoTool())
    return Engine(
        Config(
            provider=p,
            registry=reg,
            composer=Composer(),  # 空配置，避免读取工作目录下的 AGENTS.md
            max_turns=max_turns,
            keep_recent=50,
        )
    )


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class TestLoop(unittest.IsolatedAsyncioTestCase):
    async def test_runs_tool_then_answers(self):
        p = ScriptedProvider(
            [
                ChatResponse(
                    thinking="先回显一下",
                    tool_calls=[ToolCall(id="t1", name="echo", input='{"v":"hi"}')],
                    stop_reason=STOP_TOOL_USE,
                    model="fake-1",
                    usage=Usage(input_tokens=10, output_tokens=5),
                ),
                ChatResponse(
                    content="done",
                    stop_reason=STOP_END,
                    model="fake-1",
                    usage=Usage(input_tokens=20, output_tokens=3),
                ),
            ]
        )
        eng = _new_test_engine(p, 5)
        sess = Session(id="test", work_dir="", label="test")

        res = await eng.run(sess, "go")
        self.assertEqual(res.text, "done")
        self.assertEqual(res.turns, 2)
        self.assertFalse(res.exceeded)
        self.assertEqual(res.usage.input_tokens, 30)
        self.assertEqual(res.usage.output_tokens, 8)

        # 历史必须包含：用户 → 助手(带工具调用) → 工具回执 → 助手(最终答案)
        hist = sess.history()
        self.assertEqual(len(hist), 4)
        self.assertEqual(hist[2].role, Role.TOOL)
        self.assertIn("echo:", hist[2].content)

        # 两轮请求都必须带上工具声明与系统提示。
        for i, req in enumerate(p.requests):
            self.assertTrue(req.tools, f"request {i} missing tool specs")
            self.assertTrue(req.system, f"request {i} missing system prompt")

        # 第二轮应能看到第一轮的工具回执。
        last = p.requests[-1]
        self.assertTrue(any(m.role == Role.TOOL for m in last.messages))

    async def test_stops_at_max_turns(self):
        # Provider 永远返回工具调用 → 必须被轮次上限兜住，而不是无限烧钱。
        p = ScriptedProvider()
        eng = _new_test_engine(p, 3)
        sess = Session(id="test", work_dir="", label="test")

        res = await eng.run(sess, "go")
        self.assertTrue(res.exceeded)
        self.assertEqual(res.turns, 3)
        self.assertNotEqual(res.text, "")

    async def test_rejects_empty_input(self):
        eng = _new_test_engine(ScriptedProvider(), 5)
        sess = Session(id="test", work_dir="", label="test")
        with self.assertRaises(EngineError):
            await eng.run(sess, "   ")

    async def test_requires_provider(self):
        eng = Engine(Config(composer=Composer()))
        sess = Session(id="test", work_dir="", label="test")
        with self.assertRaises(EngineError):
            await eng.run(sess, "go")

    async def test_concurrent_tool_results_keep_order(self):
        """一轮里多个工具调用并发执行，但回执必须按调用顺序回填。"""
        calls = [
            ToolCall(id="c1", name="echo", input='{"v":"1"}'),
            ToolCall(id="c2", name="echo", input='{"v":"2"}'),
            ToolCall(id="c3", name="echo", input='{"v":"3"}'),
        ]
        p = ScriptedProvider(
            [
                ChatResponse(tool_calls=calls, stop_reason=STOP_TOOL_USE, model="fake-1"),
                ChatResponse(content="ok", stop_reason=STOP_END, model="fake-1"),
            ]
        )
        eng = _new_test_engine(p, 5)
        sess = Session(id="test", work_dir="", label="test")
        res = await eng.run(sess, "go")

        self.assertEqual(res.text, "ok")
        hist = sess.history()
        # 用户 / 助手 / 三个回执(按序) / 最终助手 = 6 条
        self.assertEqual(len(hist), 6)
        ids = [m.tool_call_id for m in hist if m.role == Role.TOOL]
        self.assertEqual(ids, ["c1", "c2", "c3"])


if __name__ == "__main__":
    unittest.main()
