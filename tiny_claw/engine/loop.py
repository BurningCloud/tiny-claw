"""引擎核心：手写 ReAct 主循环。

对应专栏第 2、3 讲。整个引擎只有一件事在做：

    组装上下文 → 问模型 → 有工具调用就执行 → 把结果喂回去 → 再问
                  ↑                                          │
                  └──────────────────────────────────────────┘

听起来简单，但"工业级"和"Demo 级"的分界线全在这个循环的细节里：
上下文什么时候压缩、工具失败怎么回喂、怎样发现自己已经原地打转、
什么时候该认输停下来。这些全部由 Harness 决定，模型只负责思考。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from tiny_claw import context as clawctx
from tiny_claw.observability import Accountant, Event, EventKind, Tracer, new_noop_tracer
from tiny_claw.provider import (
    ChatRequest,
    ChatResponse,
    ContextTooLongError,
    EmptyResponseError,
    Provider,
)
from tiny_claw.schema import Message, Role, ToolCall, Usage
from tiny_claw.stability import (
    BlockedError,
    RecoveryAdvisor,
    Reminder,
    build_heartbeat,
    build_loop_alert,
)
from tiny_claw.tools import Execution, Registry


class EngineError(RuntimeError):
    """引擎无法继续运行（模型调用失败、配置缺失等）。"""


@dataclass
class Progress:
    """一轮的进展快照，用于 CLI 输出或 IM 推送。"""

    turn: int
    thinking: str = ""
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    executions: list[Execution] = field(default_factory=list)
    # compaction 为本轮生效的压缩档位。
    compaction: clawctx.Level = clawctx.Level.NONE


@dataclass
class Result:
    """一次运行的最终结果。"""

    text: str = ""
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    duration_ms: int = 0
    # exceeded 表示达到了 max_turns 上限仍未收尾。
    exceeded: bool = False


@dataclass
class Config:
    """引擎配置。"""

    provider: Provider | None = None
    registry: Registry | None = None
    composer: clawctx.Composer | None = None
    compactor: clawctx.Compactor | None = None
    memory: clawctx.FileMemory | None = None

    # tracer / accountant 传 None 时引擎自动降级为 no-op。
    tracer: Tracer | None = None
    accountant: Accountant | None = None
    # advisor 为错误自愈顾问，None 表示自动创建默认的。
    advisor: RecoveryAdvisor | None = None

    work_dir: str = ""
    # max_turns 是单任务最大轮次，防止失控烧钱。
    max_turns: int = 0
    max_tokens: int = 0
    temperature: float = 0.0
    thinking: bool = False
    thinking_budget: int = 0
    keep_recent: int = 0
    loop_threshold: int = 0
    heartbeat_every: int = 0

    on_progress: Callable[[Progress], None] | None = None


class Engine:
    """ReAct 引擎。"""

    def __init__(self, cfg: Config) -> None:
        # ── 补齐默认值（对应 Go 版 NewEngine）────────────────────
        if cfg.max_turns <= 0:
            cfg.max_turns = 40
        if cfg.max_tokens <= 0:
            cfg.max_tokens = 8192
        if cfg.keep_recent <= 0:
            cfg.keep_recent = 40
        if cfg.loop_threshold <= 0:
            cfg.loop_threshold = 3
        if not cfg.work_dir:
            import os

            cfg.work_dir = os.getcwd()
        if cfg.composer is None:
            cfg.composer = clawctx.Composer(work_dir=cfg.work_dir)
        if cfg.compactor is None:
            cfg.compactor = clawctx.Compactor(clawctx.default_policy(120000))
        if cfg.registry is None:
            cfg.registry = Registry()
        if cfg.advisor is None:
            cfg.advisor = RecoveryAdvisor()

        self.cfg = cfg
        # tracer / accountant 为 None 时降级为 no-op，避免调用方到处判空。
        self._tracer = cfg.tracer if cfg.tracer is not None else new_noop_tracer()
        self._accountant = cfg.accountant if cfg.accountant is not None else Accountant(None)

        # 子 Agent 工具集：除了 spawn_subagent 以外全部继承。
        # 否则子 Agent 又能开子 Agent，形成无限递归。
        self._sub_registry = Registry()
        for name, tool in cfg.registry.all().items():
            if name == "spawn_subagent":
                continue
            self._sub_registry.register(tool)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def run(self, sess: clawctx.Session, user_input: str) -> Result:
        """执行一次完整的 ReAct 任务。"""
        return await self._run_with(sess, user_input, Reminder(self.cfg.loop_threshold))

    async def run_subagent(self, task: str) -> str:
        """在隔离的上下文里跑一个探索性任务，只返回结论摘要。

        这是"上下文隔离"的落点：子 Agent 读多少文件都不影响主会话，
        因为它的历史根本不在主 Session 里。
        """
        sess = clawctx.Session(
            id=f"sub-{time.time_ns()}", work_dir=self.cfg.work_dir, label="subagent"
        )
        # 子 Agent 的指令刻意要求"只给结论"：
        # 否则它把探索过程也吐出来，隔离就白做了。
        prompt = (
            "你是一个探索子 Agent。请完成下面的任务，并且**只输出结论**，\n"
            "不要描述你的探索过程，不要粘贴大段代码原文"
            "（关键片段除外，控制在 20 行内）。\n\n任务：\n" + task
        )
        res = await self._run_with(sess, prompt, Reminder(self.cfg.loop_threshold))
        return res.text if res.text else "(子 Agent 未产出结论)"

    def tracer(self) -> Tracer:
        """暴露当前 Tracer，便于收尾打印链路。"""
        return self._tracer

    def accountant(self) -> Accountant:
        """暴露记账器。"""
        return self._accountant

    def sub_registry(self) -> Registry:
        """子 Agent 可用的工具集（去掉 spawn_subagent 防递归）。"""
        return self._sub_registry

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def _run_with(
        self, sess: clawctx.Session, user_input: str, reminder: Reminder
    ) -> Result:
        """真正的循环实现。

        reminder 每次运行独立创建，这样并发的多个 Session 之间
        不会互相污染死循环检测状态。
        """
        if not user_input.strip():
            raise EngineError("engine: empty user input")
        if self.cfg.provider is None:
            raise EngineError("engine: provider is not configured")

        started = time.monotonic()
        res = Result()

        sess.append(Message(role=Role.USER, content=user_input, pinned=True))
        self._record(sess, 0, EventKind.USER_INPUT, user_input, True)

        # sections 是本轮要额外注入系统提示的运行时信息（提醒、自愈指南）。
        # 它们不进历史，只影响当轮 —— 否则提醒会越积越多，反而污染上下文。
        sections: list[str] = []

        max_turns = self.cfg.max_turns
        for turn in range(1, max_turns + 1):
            # ── 1. 动态组装系统提示 ────────────────────────────────
            sys_prompt = self.cfg.composer.compose(*sections)

            # ── 2. 压缩 + 取工作记忆窗口 ───────────────────────────
            level = self._compact_if_needed(sess)
            window = clawctx.working_memory(sess.history(), self.cfg.keep_recent)

            # ── 3. 问模型（含独立的慢思考阶段）─────────────────────
            req = ChatRequest(
                system=sys_prompt,
                messages=window,
                tools=self.cfg.registry.specs(),
                max_tokens=self.cfg.max_tokens,
                temperature=self.cfg.temperature,
                thinking=self.cfg.thinking,
                thinking_budget=self.cfg.thinking_budget,
            )
            try:
                resp = await self.cfg.provider.chat(req)
            except ContextTooLongError:
                # 紧急瘦身：把历史砍掉一半再试一次，而不是让整个任务崩掉。
                self._record(sess, turn, EventKind.COMPACT, "上下文超限，紧急裁剪", False)
                sess.set_history(
                    clawctx.working_memory(sess.history(), self.cfg.keep_recent // 2 + 2)
                )
                continue
            except EmptyResponseError:
                # 与 Go 源的偏差说明：Go 版 Claude 实现返回 (resp, ErrEmptyResponse)，
                # 而 OpenAI 实现有一处返回 (nil, ErrEmptyResponse)，此时引擎会走进
                # "nil response" 分支直接报错退出。那与接口注释声明的意图
                # （"引擎遇到它会注入提醒并重试，而不是直接崩掉"）相矛盾，
                # 属于 Go 源自身的不一致。这里统一按声明的意图处理：
                # 视为一次空回复，交给下面的空内容分支注入提醒后重试。
                resp = ChatResponse()
            except Exception as exc:  # noqa: BLE001 - 任何模型调用异常都要记账后抛出
                self._record(sess, turn, EventKind.ERROR, str(exc), False)
                raise EngineError(
                    f"engine: model call failed at turn {turn}: {exc}"
                ) from exc

            # 慢思考是这一轮的"规划"，单独入账、单独记录。
            if resp.thinking:
                self._record(sess, turn, EventKind.THINKING, _first_chars(resp.thinking, 80), True)
            self._accountant.record(resp.model, resp.usage, resp.latency_ms)
            res.usage.input_tokens += resp.usage.input_tokens
            res.usage.output_tokens += resp.usage.output_tokens
            self._record(
                sess,
                turn,
                EventKind.MODEL,
                f"{resp.model} in={resp.usage.input_tokens} "
                f"out={resp.usage.output_tokens} {resp.latency_ms}ms",
                True,
            )

            sess.append(
                Message(
                    role=Role.ASSISTANT,
                    content=resp.content,
                    thinking=resp.thinking,
                    tool_calls=resp.tool_calls,
                )
            )

            # ── 4. 没有工具调用 → 这一轮就是答案 ───────────────────
            if not resp.tool_calls:
                if not resp.content:
                    # 空回复：注入提醒再来一轮，而不是把空串当用户答案。
                    sections = [
                        "## 运行时提醒\n\n上一轮你没有产出任何内容，也没有调用工具。"
                        "请直接给出结论或明确说明你需要什么信息。"
                    ]
                    continue
                res.text = resp.content
                res.turns = turn
                res.duration_ms = int((time.monotonic() - started) * 1000)
                self._record(
                    sess, turn, EventKind.FINAL, _first_chars(resp.content, 80), True
                )
                self._emit(
                    Progress(
                        turn=turn,
                        thinking=resp.thinking,
                        content=resp.content,
                        compaction=level,
                    )
                )
                return res

            # ── 5. 并发执行工具 ────────────────────────────────────
            execs = await self.cfg.registry.execute_all(resp.tool_calls)
            err_lines: list[str] = []
            for ex in execs:
                # 被拦截的执行要打上标记，让它以"拦截"而非"崩溃"的姿态回喂。
                if ex.err is not None and isinstance(ex.err, BlockedError):
                    ex.blocked = True
                if ex.blocked:
                    self._record(
                        sess, turn, EventKind.BLOCK, ex.name, False, ex.duration_ms
                    )
                else:
                    self._record(
                        sess,
                        turn,
                        EventKind.TOOL,
                        f"{ex.name} → {_first_chars(ex.result(), 90)}",
                        ex.err is None,
                        ex.duration_ms,
                    )
                if ex.err is not None and not ex.blocked:
                    err_lines.append(ex.result())
                # 工具回执必须按调用顺序回填，否则模型无法对号入座。
                sess.append(
                    Message(
                        role=Role.TOOL,
                        content=ex.result(),
                        name=ex.name,
                        tool_call_id=ex.tool_call_id,
                        is_error=ex.is_error(),
                    )
                )

            # ── 6. 死循环检测（运行时干预）─────────────────────────
            sections = []
            if reminder.observe(resp.tool_calls):
                sections.append(build_loop_alert(turn))
                self._record(sess, turn, EventKind.REMINDER, "检测到死循环，注入打断指令", True)

            # ── 7. 错误自愈：把报错翻译成下一步指引 ────────────────
            if self.cfg.advisor is not None and err_lines:
                advice = self.cfg.advisor.advise("\n".join(err_lines))
                if advice:
                    sections.append(advice)
                    self._record(sess, turn, EventKind.REMINDER, "注入错误自愈提示", True)

            # ── 8. 长任务心跳：定期把模型拉回主线 ──────────────────
            if self.cfg.heartbeat_every > 0 and turn % self.cfg.heartbeat_every == 0:
                sections.append(build_heartbeat(turn))

            self._emit(
                Progress(
                    turn=turn,
                    thinking=resp.thinking,
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                    executions=execs,
                    compaction=level,
                )
            )

        # 达到轮次上限：认输，并明确告诉调用方，不要假装成功。
        res.turns = max_turns
        res.exceeded = True
        res.duration_ms = int((time.monotonic() - started) * 1000)
        if not res.text:
            res.text = f"（已达最大轮次 {max_turns} 仍未收尾，已主动停止）"
        self._record(sess, max_turns, EventKind.ERROR, "max turns exceeded", False)
        return res

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _compact_if_needed(self, sess: clawctx.Session) -> clawctx.Level:
        """在超水位时压缩历史，返回本轮生效的档位。"""
        compacted, level = self.cfg.compactor.compact(sess.history())
        if level == clawctx.Level.NONE:
            return level
        sess.set_history(compacted)
        self._record(sess, 0, EventKind.COMPACT, f"compress level={level}", True)
        return level

    def _emit(self, p: Progress) -> None:
        if self.cfg.on_progress is not None:
            self.cfg.on_progress(p)

    def _record(
        self,
        sess: clawctx.Session,
        turn: int,
        kind: EventKind,
        detail: str,
        ok: bool = True,
        duration_ms: int = 0,
    ) -> None:
        self._tracer.record(
            Event(
                session=sess.id,
                turn=turn,
                kind=kind,
                detail=detail,
                duration_ms=duration_ms,
                ok=ok,
            )
        )


def _first_chars(s: str, n: int) -> str:
    """压缩成单行并截断，用于 Tracing 摘要。"""
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"
