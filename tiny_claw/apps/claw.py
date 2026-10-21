"""claw：交互式 CLI 入口（专栏第 6 章实战一）。

用法示例::

    export ANTHROPIC_API_KEY=sk-...
    python -m tiny_claw.apps.claw

    # 一次性任务
    python -m tiny_claw.apps.claw -prompt "找出 loop.go 里的并发 bug 并修掉"

    # 接入 OpenAI 兼容端点（DeepSeek / 通义 / vLLM / Ollama）
    python -m tiny_claw.apps.claw -provider openai -model deepseek-chat \
        -base-url https://api.deepseek.com
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from tiny_claw import bootstrap, context as clawctx
from tiny_claw.engine import Progress, Result
from tiny_claw.observability import Event, EventKind
from tiny_claw.stability import Approver, StaticApprover


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claw", description="go-tiny-claw 的 Python 版 CLI")
    p.add_argument("-workdir", default="", help="工作目录，默认当前目录（也是工具可访问的根目录）")
    p.add_argument("-provider", default="openai", help="模型提供方：claude | openai")
    p.add_argument("-model", default="", help="模型标识，留空用 Provider 默认值")
    p.add_argument("-base-url", default="", help="自定义 API 端点（自建网关 / OpenAI 兼容实现）")
    p.add_argument("-prompt", default="", help="一次性任务；留空进入交互模式")
    p.add_argument("-max-turns", type=int, default=40, help="单个任务的最大 ReAct 轮次")
    p.add_argument("-context-window", type=int, default=120000, help="模型上下文窗口预算（token）")
    p.add_argument("-thinking", action="store_true", default=True, help="启用慢思考（独立 Reasoning 阶段）")
    p.add_argument("-yolo", action="store_true", help="自动批准高危操作（Deny 级规则仍然生效）")
    p.add_argument("-approve", default="ask", help="审批模式：ask（终端询问）| auto | deny")
    p.add_argument("-no-sandbox", action="store_true", help="不限制工具的文件访问范围（谨慎）")
    return p


def _new_approver(mode: str) -> Approver | None:
    """决定人工审批通道。返回 None 表示交给 bootstrap 用默认的终端审批。"""
    if mode == "auto":
        return StaticApprover(True)
    if mode == "deny":
        return StaticApprover(False)
    return None


def _print_progress(p: Progress) -> None:
    if p.thinking:
        print(f"\n[思考] {_clip(p.thinking, 400)}")
    if p.content:
        print(f"\n{p.content}")
    for i, tc in enumerate(p.tool_calls):
        icon = "✓"
        dur = 0
        if i < len(p.executions):
            if p.executions[i].is_error():
                icon = "✗"
            dur = p.executions[i].duration_ms
        print(f"  {icon} {tc.name} {_short_input(tc.input)} ({dur}ms)")
    if p.compaction != clawctx.Level.NONE:
        print(f"  [上下文压缩: {p.compaction}]")


def _print_stats(res: Result | None, app: bootstrap.App) -> None:
    print()
    if res is not None:
        print(f"轮次 {res.turns} | 耗时 {res.duration_ms}ms")
    print(app.accountant.snapshot())
    print(f"\n链路追踪: {app.tracer.path()}")


def _print_help() -> None:
    print(
        """命令：
  /help     显示本帮助
  /reset    清空当前会话历史
  /cost     打印累计 token 与成本
  /trace    打印最近的链路事件
  /plan     打印 PLAN.md / TODO.md
  /exit     退出

行为说明：
  - 工具默认只能访问工作目录内的文件，跨目录会被拒绝（除非 -no-sandbox）。
  - Ask 级高危操作（git push、装依赖等）会停下来等你确认。
  - Deny 级操作（rm -rf /、fork bomb 等）无论如何都会被拒绝。
  - 长任务每 8 轮会收到一次"回到主线"的提醒。
  - 连续 3 次完全相同的工具调用会被判定为死循环并强制打断。"""
    )


def _short_input(s: str) -> str:
    one = " ".join((s or "").split())
    return one if len(one) <= 60 else one[:60] + "…"


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


async def _repl(app: bootstrap.App, sess: clawctx.Session) -> None:
    """交互式主循环。input() 会阻塞事件循环，因此丢到线程里跑。"""
    while True:
        print("\n> ", end="", flush=True)
        try:
            line = await asyncio.to_thread(sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:  # EOF
            print()
            return
        line = line.strip()
        if not line:
            continue

        if line in ("/exit", "/quit", "exit", "quit"):
            _print_stats(None, app)
            return
        if line == "/help":
            _print_help()
            continue
        if line == "/reset":
            sess.reset()
            print("(会话已清空)")
            continue
        if line == "/cost":
            print(app.accountant.snapshot())
            continue
        if line == "/trace":
            print(app.tracer.render(60))
            continue
        if line == "/plan":
            plan = app.memory.load_plan()
            todo = app.memory.load_todo()
            if not plan and not todo:
                print("(尚无 PLAN.md / TODO.md)")
            else:
                if plan:
                    print("── PLAN.md ──\n" + plan)
                if todo:
                    print("\n── TODO.md ──\n" + todo)
            continue

        try:
            res = await app.engine.run(sess, line)
        except Exception as exc:  # 单轮失败不影响整个会话
            print(f"错误: {exc}", file=sys.stderr)
            continue
        if res.text:
            print(f"\n{res.text}")
        if res.exceeded:
            print(f"\n(警告：任务在第 {res.turns} 轮达到上限，未正常收尾)")


async def _amain(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    app = bootstrap.new(
        bootstrap.Options(
            provider=bootstrap.ProviderOptions(
                kind=args.provider, model=args.model, base_url=args.base_url
            ),
            work_dir=args.workdir,
            sandbox=not args.no_sandbox,
            max_turns=args.max_turns,
            context_window=args.context_window,
            thinking=args.thinking,
            yolo=args.yolo,
            approver=_new_approver(args.approve),
            on_progress=_print_progress,
        )
    )
    try:
        sess = app.cli_session()
        app.tracer.record(
            Event(
                session=sess.id,
                turn=0,  # 会话级事件，不归属任何轮次（与 loop 的 COMPACT 一致）
                kind=EventKind.SESSION_START,
                detail=f"cli start workdir={app.work_dir}",
                ok=True,
            )
        )
        print(
            f"tiny-claw 已启动 | workdir={app.work_dir} | "
            f"turns<={args.max_turns} | sandbox={not args.no_sandbox}"
        )
        print("输入 /help 查看命令，/exit 退出。")

        if args.prompt.strip():
            res = await app.engine.run(sess, args.prompt)
            print(f"\n──── 结果 ────\n{res.text}")
            _print_stats(res, app)
            return 0

        await _repl(app, sess)
        return 0
    finally:
        app.close()


def main() -> None:
    try:
        code = asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n(claw: 收到中断，正在收尾)")
        code = 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层统一兜底
        print(f"claw: {exc}", file=sys.stderr)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    os.environ["OPENAI_API_KEY"] = ""
    main()
