"""可观测性层：链路追踪（Tracing）。

对应专栏第 5 章。这个包回答一个很朴素但致命的问题：
"Agent 刚才到底干了什么？"

没有 Tracing 的 Agent 就是一个黑盒：它跑了 40 轮、花了 3 块钱、最后说"改好了"，
你完全无从判断它是真的推理清楚了，还是瞎猫碰上死耗子。有了 Tracing，
失败路径可以被完整复盘——这就是本项目把"注释当作价值一半"的原因。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 事件类型
# ---------------------------------------------------------------------------

class EventKind(str, Enum):
    """事件类型。

    继承 str 是为了让 json.dumps 直接输出其字面值，不必到处写 .value。
    取值与 Go 源码保持一致（见 CONTRACT.md 第 6 节，名字一个字不许改）。
    """

    SESSION_START = "session_start"
    USER_INPUT = "user_input"
    MODEL = "model_call"
    THINKING = "thinking"
    TOOL = "tool_call"
    BLOCK = "blocked"
    COMPACT = "compaction"
    REMINDER = "reminder"
    ERROR = "error"
    FINAL = "final"


# ---------------------------------------------------------------------------
# 事件结构
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """一条链路事件。

    Name 是事件主体的短名（多为工具名 / 模型名），Detail 是具体摘要。
    两者分开是为了让 Render 既能按主体扫读，又能看到细节。
    """

    session: str
    turn: int
    kind: EventKind
    detail: str
    name: str = ""
    duration_ms: int = 0
    ok: bool = True
    tokens_in: int = 0
    tokens_out: int = 0
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        """序列化为一行 JSON（JSONL 落盘用）。

        kind 是 str 枚举，json 会直接输出其字面值。
        """
        return json.dumps(
            {
                "session": self.session,
                "turn": self.turn,
                "kind": self.kind,
                "detail": self.detail,
                "duration_ms": self.duration_ms,
                "ok": self.ok,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "ts": self.ts,
            },
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------

class Tracer:
    """收集并持久化链路事件。

    落盘格式选 JSONL 而不是 JSON 数组：Agent 可能跑到一半被 Ctrl-C，
    逐行追加能保证已经发生的事件不丢——这是可观测性最基本的可靠性要求。
    """

    def __init__(self, directory: str | None = None) -> None:
        """创建 Tracer。

        directory 为存放目录（通常是 .claw）。
        - 传 None ⇒ no-op：不落盘，但事件仍按 Go 的 NewNoopTracer 语义留在内存里，
          方便测试时断言；调用方也可以显式 new_noop_tracer()。
        - 传 ""    ⇒ 退回默认目录 ".claw"（与 Go 的 NewTracer("") 行为一致）。
        """
        self._mu = threading.Lock()
        self._path: str | None = None
        self._f: Any = None
        self._events: list[Event] = []

        if directory is None:
            return  # no-op：不打开任何文件

        if directory == "":
            directory = ".claw"

        os.makedirs(directory, exist_ok=True)
        self._path = os.path.join(directory, "trace.jsonl")
        # O_APPEND | O_CREATE | O_WRONLY，权限 0o644
        self._f = open(self._path, "a", encoding="utf-8")

    def record(self, ev: Event) -> None:
        """记录一条事件。"""
        if ev.ts == 0.0:
            ev.ts = time.time()

        with self._mu:
            self._events.append(ev)
            if self._f is not None:
                try:
                    self._f.write(ev.to_json() + "\n")
                    self._f.flush()
                except OSError:
                    # 落盘失败不应打断 Agent 主流程：Tracing 是旁路观察者。
                    pass

    def events(self) -> list[Event]:
        """返回内存中的事件快照（拷贝，避免外部篡改）。"""
        with self._mu:
            return list(self._events)

    def close(self) -> None:
        """关闭落盘文件。"""
        with self._mu:
            if self._f is not None:
                self._f.close()
                self._f = None

    def path(self) -> str | None:
        """返回落盘路径（no-op 时为 None）。"""
        return self._path

    def render(self, max_events: int = 40) -> str:
        """生成人类可读的时序摘要，用于收尾打印或写进复盘报告。

        max_events > 0 时只取最近 N 条，避免超长链路刷屏。
        """
        evs = self.events()
        if max_events > 0 and len(evs) > max_events:
            evs = evs[len(evs) - max_events:]

        lines: list[str] = []
        lines.append("时间      轮次  类型            名称 / 摘要")
        lines.append("────────────────────────────────────────────────────────")
        for e in evs:
            mark = "✓" if e.ok else "✗"
            # 有名称时拼成「名称 · 摘要」；名称为空时直接显示摘要，
            # 避免渲染出多余的引导分隔符。
            if e.name and e.detail:
                detail = f"{e.name} · {e.detail}"
            else:
                detail = e.name or e.detail
            ts = time.strftime("%H:%M:%S", time.localtime(e.ts))
            lines.append(
                "%s  %-4d %-14s  %s %s"
                % (ts, e.turn, e.kind.value, mark, clip_runes(detail, 60))
            )
        return "\n".join(lines)


def new_noop_tracer() -> Tracer:
    """创建只进内存、不落盘的 Tracer，用于测试。"""
    return Tracer(None)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def clip_runes(s: str, max_chars: int) -> str:
    """按"字符（rune）"截断，避免把中文/emoji 切成乱码。"""
    runes = list(s)
    if len(runes) <= max_chars:
        return s
    return "".join(runes[:max_chars]) + "…"
