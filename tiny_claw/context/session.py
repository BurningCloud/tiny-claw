"""会话管理（Session / Manager）。

对应专栏第 11 讲。

为什么必须做物理隔离：如果只有一个全局历史切片，那么「飞书 A 群让 Agent
重构 main.go」和「B 群让 Agent 查日志」的指令与文件内容会混在一条上下文里
发给模型——模型立刻精神分裂。Session 就是解决这个的：
一个会话 = 一条上下文 = 一个工作目录。

本层方法均为同步（asyncio 是协作式单线程，无需锁即可保证历史不被并发改写）。
"""

from __future__ import annotations

import hashlib
import time

from tiny_claw.schema.message import Message

# 模块级常量，避免与内置 id() 混淆。


class Session:
    """一次独立的会话，持有自己的历史与工作目录。"""

    def __init__(self, id: str, work_dir: str, label: str) -> None:
        self.id: str = id
        self.work_dir: str = work_dir
        # Label 是人类可读的标识，如 "cli" 或 "feishu:oc_xxx"。
        self.label: str = label
        # messages 是会话内部历史存储；对外通过 history() 返回快照，
        # 因此此处不能用 "history" 这个名（与 history() 方法冲突）。
        self.messages: list[Message] = []
        self.created_at: float = time.time()
        self.updated_at: float = time.time()
        # TurnCount 用于运行时提醒与 Benchmark 统计。
        self.turn_count: int = 0

    def append(self, *msgs: Message) -> None:
        """追加消息。"""
        self.messages.extend(msgs)
        self.updated_at = time.time()

    def history(self) -> list[Message]:
        """返回历史快照（副本）。

        必须返回副本而非内部 list 本身：引擎会基于快照做压缩、改写，
        若直接返回内部引用，引擎对历史的任何改动都会污染 Session 状态。
        """
        return list(self.messages)

    def set_history(self, msgs: list[Message]) -> None:
        """整体替换历史，供压缩器回写使用。"""
        self.messages = msgs
        self.updated_at = time.time()

    def bump_turn(self) -> int:
        """递增轮次计数并返回新值。"""
        self.turn_count += 1
        return self.turn_count

    def reset(self) -> None:
        """清空历史与轮次计数。"""
        self.messages = []
        self.turn_count = 0
        self.updated_at = time.time()


class Manager:
    """管理多个会话。

    会话 ID 的来源决定隔离粒度：
      - CLI：工作目录的哈希（一个目录一个会话）
      - 飞书/IM：chat_id（一个群一个会话）
    """

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}

    def get(self, id: str) -> Session | None:
        """返回已有会话，不存在返回 None。"""
        return self.sessions.get(id)

    def get_or_create(self, id: str, work_dir: str, label: str) -> Session:
        """取会话，不存在则创建。"""
        if (s := self.sessions.get(id)) is not None:
            return s
        s = Session(id, work_dir, label)
        self.sessions[id] = s
        return s

    def delete(self, id: str) -> None:
        """移除会话。"""
        self.sessions.pop(id, None)

    def list(self) -> list[str]:
        """返回所有会话 ID。"""
        return list(self.sessions.keys())


def session_id_for_dir(path: str) -> str:
    """由工作目录派生稳定的会话 ID。"""
    digest = hashlib.sha256(("dir:" + path).encode("utf-8")).hexdigest()
    return "dir-" + digest[:12]


def session_id_for_chat(platform: str, chat_id: str) -> str:
    """由 IM 会话标识派生会话 ID。"""
    return f"{platform}-{chat_id}"
