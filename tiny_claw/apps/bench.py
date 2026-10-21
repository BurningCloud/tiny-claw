"""bench：Harness 的自动化评估脚本（专栏第 20 讲）。

为什么要写它：没有量化就没有改进。改了一版压缩策略、换了一个模型、
调整了提醒措辞——到底变好还是变差了？靠"感觉挺顺的"是没法做工程决策的。

用法::

    python -m tiny_claw.apps.bench -workdir ./
    python -m tiny_claw.apps.bench -provider openai -model deepseek-chat

任务集写在 tasks.py 里，建议按自己的项目改写——通用的任务集
只能衡量通用能力，衡量不了你真正关心的那件事。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass

from tiny_claw import bootstrap
from tiny_claw.apps.tasks import Task, default_tasks


@dataclass
class Row:
    """一个任务的评估结果。"""

    task: Task
    passed: bool = False
    turns: int = 0
    elapsed_ms: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    note: str = ""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench", description="tiny-claw 自动化评估")
    p.add_argument("-provider", default="claude", help="claude | openai")
    p.add_argument("-model", default="", help="模型标识")
    p.add_argument("-base-url", default="", help="自定义端点")
    p.add_argument("-workdir", default="", help="被测项目目录，默认当前目录")
    p.add_argument("-max-turns", type=int, default=12, help="每个任务的轮次上限")
    p.add_argument("-context-window", type=int, default=120000, help="上下文窗口预算")
    p.add_argument("-thinking", action="store_true", default=True, help="启用慢思考")
    p.add_argument("-only", default="", help="只跑名字包含该子串的任务")
    p.add_argument("-v", action="store_true", help="打印每个任务的最终回答")
    return p


def _missing_keywords(text: str, expect: list[str]) -> list[str]:
    lower = text.lower()
    return [e for e in expect if e.lower() not in lower]


def _pct(a: int, b: int) -> float:
    return 0.0 if b == 0 else a / b * 100


def _avg(v: float, n: int) -> float:
    return 0.0 if n == 0 else v / n


def _print_summary(rows: list[Row]) -> None:
    passed = sum(1 for r in rows if r.passed)
    total_turns = sum(r.turns for r in rows)
    total_tokens = sum(r.tokens for r in rows)
    total_cost = sum(r.cost_usd for r in rows)
    total_ms = sum(r.elapsed_ms for r in rows)
    n = len(rows)

    print("\n════ 汇总 ════")
    print(f"通过率   {passed}/{n} ({_pct(passed, n):.0f}%)")
    print(f"总轮次   {total_turns}（平均 {_avg(total_turns, n):.1f}/任务）")
    print(f"总 token {total_tokens}（平均 {_avg(total_tokens, n):.0f}/任务）")
    print(f"总成本   ${total_cost:.4f}（平均 ${_avg(total_cost, n):.4f}/任务）")
    print(f"总耗时   {total_ms}ms")


async def _amain(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    app = bootstrap.new(
        bootstrap.Options(
            provider=bootstrap.ProviderOptions(
                kind=args.provider, model=args.model, base_url=args.base_url
            ),
            work_dir=args.workdir,
            sandbox=True,
            max_turns=args.max_turns,
            context_window=args.context_window,
            thinking=args.thinking,
            # Benchmark 是无人值守的，Ask 级一律放行、Deny 级仍然拦截。
            yolo=True,
            # 评估跑在同一个目录时共享 .claw，方便事后对比 trace。
            claw_dir="",
        )
    )

    tasks = default_tasks()
    if args.only:
        tasks = [t for t in tasks if args.only in t.name]
    if not tasks:
        print("没有匹配的任务")
        app.close()
        return 0

    print(
        f"tiny-claw benchmark | tasks={len(tasks)} | "
        f"workdir={app.work_dir} | turns<={args.max_turns}\n"
    )

    rows: list[Row] = []
    try:
        for t in tasks:
            # 每个任务用独立会话，避免上一个任务的上下文影响下一个。
            sess = app.session("bench-" + t.name, "bench")
            before = app.accountant.snapshot()

            start = time.monotonic()
            err: Exception | None = None
            res = None
            try:
                res = await app.engine.run(sess, t.prompt)
            except Exception as exc:  # noqa: BLE001 - 单任务失败不中断整轮评估
                err = exc
            elapsed_ms = int((time.monotonic() - start) * 1000)

            after = app.accountant.snapshot()
            r = Row(
                task=t,
                elapsed_ms=elapsed_ms,
                tokens=(after.input_tokens + after.output_tokens)
                - (before.input_tokens + before.output_tokens),
                cost_usd=after.cost_usd - before.cost_usd,
            )

            if err is not None:
                r.note = f"执行错误: {err}"
            elif res is None:
                r.note = "无结果"
            else:
                r.turns = res.turns
                if res.exceeded:
                    r.note = "超出轮次上限"
                else:
                    missing = _missing_keywords(res.text, t.expect)
                    if not missing:
                        r.passed = True
                    else:
                        r.note = "缺少关键词: " + ", ".join(missing)
                if args.v:
                    print(f"── {t.name} 回答 ──\n{res.text}\n")

            rows.append(r)
            status = "PASS" if r.passed else "FAIL"
            print(
                f"[{status}] {t.name:<12} turns={r.turns:<3} {r.elapsed_ms}ms   "
                f"tokens={r.tokens:<7} ${r.cost_usd:.4f}"
            )
            if r.note:
                print(f"        ↳ {r.note}")
    finally:
        app.close()

    _print_summary(rows)
    return 0 if all(r.passed for r in rows) else 1


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
