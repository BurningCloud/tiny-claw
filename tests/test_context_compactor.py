"""compactor_test.go 的 Python 移植（标准库 unittest，零第三方依赖）。

覆盖 Go 版测的全部边界：孤儿工具回执清理、调用-回执配对修复、
阶梯压缩触发/不触发、token 估算。
"""

from __future__ import annotations

import unittest

from tiny_claw.context.compactor import (
    Compactor,
    Level,
    Policy,
    default_policy,
    estimate_tokens,
    repair_pairs,
    working_memory,
)
from tiny_claw.schema.message import Message, Role, ToolCall


class TestWorkingMemoryDropsOrphanToolResult(unittest.TestCase):
    def test_drops(self) -> None:
        msgs = [
            Message(role=Role.USER, content="hi", pinned=True),
            Message(role=Role.ASSISTANT, tool_calls=[ToolCall(id="c1", name="bash")]),
            Message(role=Role.TOOL, tool_call_id="c1", content="ok"),
            # 这条回执对应的 assistant 调用已被压缩掉 → 孤儿，必须丢弃，
            # 否则各家 API 都会返回 400，整个会话报废。
            Message(role=Role.TOOL, tool_call_id="ghost", content="orphan"),
        ]
        got = working_memory(msgs, 10)
        self.assertEqual(len(got), 3)
        for m in got:
            self.assertNotEqual(m.tool_call_id, "ghost")


class TestRepairPairsStripsCallWithoutResult(unittest.TestCase):
    def test_strips(self) -> None:
        msgs = [
            Message(
                role=Role.ASSISTANT,
                content="let me check",
                tool_calls=[ToolCall(id="c1", name="bash")],
            ),
            Message(role=Role.USER, content="ok"),
        ]
        got = repair_pairs(msgs)
        self.assertEqual(len(got), 2)
        self.assertEqual(len(got[0].tool_calls), 0)


class TestCompactTriggers(unittest.TestCase):
    def test_triggers(self) -> None:
        pol = Policy(
            max_tokens=1000,
            mask_ratio=0.5,
            truncate_ratio=0.8,
            keep_recent=5,
            max_message_chars=50,
        )
        c = Compactor(pol)

        msgs = [Message(role=Role.USER, content="a" * 4000, pinned=True)]
        for _ in range(30):
            msgs.append(Message(role=Role.ASSISTANT, content="x"))

        if estimate_tokens(msgs[0].content) <= int(pol.max_tokens * pol.mask_ratio):
            self.fail("fixture not big enough to trigger compaction")

        out, lvl = c.compact(msgs)
        self.assertNotEqual(lvl, Level.NONE)
        self.assertLess(len(out), len(msgs))


class TestCompactNoopWhenSmall(unittest.TestCase):
    def test_noop(self) -> None:
        c = Compactor(default_policy(120000))
        msgs = [Message(role=Role.USER, content="hello")]
        _, lvl = c.compact(msgs)
        self.assertEqual(lvl, Level.NONE)


class TestEstimateTokens(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)
        # 中文按 1 字 1 token 保守估算，英文按 4 字符 1 token。
        self.assertGreaterEqual(estimate_tokens("你好世界"), 4)
        self.assertGreaterEqual(estimate_tokens("a" * 400), 90)


if __name__ == "__main__":
    unittest.main()
