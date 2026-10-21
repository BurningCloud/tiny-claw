"""装配层：把一个可运行的 Harness 拼起来。

CLI、Benchmark、飞书机器人三个入口共用这一层，
这样"加一个工具"或"换一种审批通道"只需要改一处。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from tiny_claw import context as clawctx
from tiny_claw.context import (
    Composer,
    Compactor,
    FileMemory,
    Manager,
    Session,
    default_policy,
    session_id_for_dir,
)
from tiny_claw.engine.loop import Config as EngineConfig
from tiny_claw.engine.loop import Engine, Progress
from tiny_claw.observability import Accountant, Tracer
from tiny_claw.provider import Claude, ClaudeConfig, OpenAI, OpenAIConfig, Provider
from tiny_claw.stability import (
    Approver,
    Guard,
    StaticApprover,
    TerminalApprover,
    default_classifier,
)
from tiny_claw.tools import (
    BashTool,
    EditFileTool,
    ReadFileTool,
    Registry,
    SubagentTool,
    WriteFileTool,
)


@dataclass
class ProviderOptions:
    """构造 Provider 的参数。"""

    # kind: claude | openai
    kind: str = "claude"
    # model 留空则用 Provider 默认值。
    model: str = ""
    # base_url 指向自建网关或 OpenAI 兼容实现。
    base_url: str = ""


def new_provider(o: ProviderOptions) -> Provider:
    """按环境变量与配置构造 Provider。"""
    kind = (o.kind or "claude").lower()
    if kind in ("", "claude", "anthropic"):
        key = _first_env("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")
        if not key and not o.base_url:
            raise ValueError("使用 claude 需要设置 ANTHROPIC_API_KEY 环境变量")
        cfg = ClaudeConfig(api_key=key, model=o.model)
        if o.base_url:
            cfg.base_url = o.base_url
        return Claude(cfg)

    if kind in ("openai", "compat"):
        key = _first_env("OPENAI_API_KEY", "API_KEY")
        # 自建端点（Ollama / vLLM）常无鉴权，因此只在没传 base_url 时强制要求 key。
        if not key and not o.base_url:
            raise ValueError(
                "使用 openai 需要设置 OPENAI_API_KEY，或用 base_url 指向免鉴权端点"
            )
        cfg = OpenAIConfig(api_key=key, model=o.model)
        if o.base_url:
            cfg.base_url = o.base_url
        return OpenAI(cfg)

    raise ValueError(f"未知 provider {o.kind!r}（支持 claude / openai）")


@dataclass
class Options:
    """装配参数。"""

    provider: ProviderOptions = field(default_factory=ProviderOptions)
    # work_dir 是工作目录，留空取当前目录。
    work_dir: str = ""
    # sandbox 为 True 时把工具的文件访问限制在 work_dir 内。默认建议开启。
    sandbox: bool = True
    # max_turns 单任务最大轮次。
    max_turns: int = 0
    # context_window 上下文窗口预算。
    context_window: int = 0
    thinking: bool = False
    # heartbeat_every 进度提醒间隔轮数，0 关闭。
    heartbeat_every: int = 0
    # approver 是人工审批通道；None 时按 yolo 决定。
    approver: Approver | None = None
    # yolo 自动批准 Ask 级操作（Deny 级仍然拦截）。
    yolo: bool = False
    # on_progress 渲染每轮进展。
    on_progress: Callable[[Progress], None] | None = None
    # claw_dir 是 .claw 目录位置，留空为 work_dir/.claw。
    claw_dir: str = ""


@dataclass
class App:
    """装配好的运行时。"""

    engine: Engine
    registry: Registry
    memory: FileMemory
    tracer: Tracer
    accountant: Accountant
    sessions: Manager
    work_dir: str
    claw_dir: str

    def close(self) -> None:
        """释放资源（目前主要是关闭 trace 文件）。"""
        self.tracer.close()

    def session(self, id: str, label: str) -> Session:
        """取（或建）一个会话。"""
        return self.sessions.get_or_create(id, self.work_dir, label)

    def cli_session(self) -> Session:
        """返回按工作目录隔离的默认会话。"""
        return self.session(session_id_for_dir(self.work_dir), "cli")


def new(opts: Options) -> App:
    """装配一个完整的 Harness 运行时。"""
    wd = opts.work_dir or os.getcwd()
    abs_wd = os.path.abspath(wd)

    # root 是工具可见的文件系统边界。sandbox 关闭时为空串 = 不限制。
    root = abs_wd if opts.sandbox else ""

    claw_dir = opts.claw_dir or os.path.join(abs_wd, ".claw")

    prov = new_provider(opts.provider)

    tracer = Tracer(claw_dir)
    accountant = Accountant(None)
    memory = FileMemory(claw_dir)
    composer = Composer(work_dir=abs_wd, memory=memory)

    ctx_window = opts.context_window if opts.context_window > 0 else 120000
    compactor = Compactor(default_policy(ctx_window))

    approver = opts.approver
    if approver is None:
        # Yolo 只自动放行 Ask 级；Deny 级由分类器独立拦住，与审批通道无关。
        approver = (
            StaticApprover(True) if opts.yolo else TerminalApprover(timeout=300.0)
        )

    reg = Registry()
    reg.register(ReadFileTool(root=root, max_lines=2000, max_line_chars=2000))
    reg.register(WriteFileTool(root=root))
    reg.register(EditFileTool(root=root))
    reg.register(BashTool(work_dir=abs_wd))

    # 安全中间件挂在这里：它是物理拦截，不依赖模型的自觉。
    guard = Guard(default_classifier(), approver)
    reg.use(guard.hook)

    # 长任务容易跑偏，默认每 8 轮把模型拉回主线一次。
    heartbeat = opts.heartbeat_every if opts.heartbeat_every > 0 else 8

    eng = Engine(
        EngineConfig(
            provider=prov,
            registry=reg,
            composer=composer,
            compactor=compactor,
            memory=memory,
            tracer=tracer,
            accountant=accountant,
            work_dir=abs_wd,
            max_turns=opts.max_turns,
            thinking=opts.thinking,
            heartbeat_every=heartbeat,
            on_progress=opts.on_progress,
        )
    )

    # Subagent 依赖已构造好的引擎，用闭包回填 runner。
    async def _run_sub(task: str) -> str:
        return await eng.run_subagent(task)

    reg.register(SubagentTool(runner=_run_sub))

    return App(
        engine=eng,
        registry=reg,
        memory=memory,
        tracer=tracer,
        accountant=accountant,
        sessions=Manager(),
        work_dir=abs_wd,
        claw_dir=claw_dir,
    )


def _first_env(*names: str) -> str:
    """按顺序取第一个非空环境变量。"""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""
