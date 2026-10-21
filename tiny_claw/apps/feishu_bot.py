"""feishu_bot：飞书渠道入口（AgentOps 场景）。

用法::

    export FEISHU_APP_ID=cli_xxx
    export FEISHU_APP_SECRET=xxx
    python -m tiny_claw.apps.feishu_bot -workdir ./

为什么值得接 IM：Agent 的价值在"有人真的在用"。接进群里之后，
它的每一次工具调用、每一次死循环、每一次被拦截，都会变成
一条可追溯的消息——这才是 AgentOps 的起点。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from tiny_claw import bootstrap
from tiny_claw.im import Bot, FeishuConfig
from tiny_claw.stability import ApprovalRequest, Approver


class _ApproverHolder(Approver):
    """延迟绑定的审批通道。

    装配顺序上有个先有鸡还是先有蛋的问题：引擎需要审批通道，
    而飞书审批通道需要引擎（Bot 要用它跑任务）。Go 版用 holder 解决，
    这里照做：先塞一个空壳，等 bot 建好再回填。
    """

    def __init__(self) -> None:
        self._inner: Approver | None = None

    def set(self, approver: Approver) -> None:
        self._inner = approver

    async def approve(self, req: ApprovalRequest) -> bool:
        # 没有审批通道时拒绝而非放行，与 Guard 的默认策略一致。
        if self._inner is None:
            return False
        return await self._inner.approve(req)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="feishu_bot", description="tiny-claw 飞书机器人")
    p.add_argument("-app-id", default=os.environ.get("FEISHU_APP_ID", ""), help="飞书应用 App ID")
    p.add_argument("-app-secret", default=os.environ.get("FEISHU_APP_SECRET", ""),
                   help="飞书应用 App Secret")
    p.add_argument("-addr", default=":8080", help="回调服务监听地址")
    p.add_argument("-path", default="/webhook", help="回调路径")
    p.add_argument("-verify-token", default=os.environ.get("FEISHU_VERIFY_TOKEN", ""),
                   help="事件校验 token")
    p.add_argument("-workdir", default="", help="Agent 的工作目录（也是沙箱根目录）")
    p.add_argument("-provider", default="claude", help="claude | openai")
    p.add_argument("-model", default="", help="模型标识")
    p.add_argument("-base-url", default="", help="自定义端点")
    p.add_argument("-max-turns", type=int, default=30, help="单任务最大轮次")
    p.add_argument("-thinking", action="store_true", default=True, help="启用慢思考")
    p.add_argument("-no-sandbox", action="store_true", help="关闭文件系统沙箱")
    return p


async def _amain(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.app_id or not args.app_secret:
        print(
            "需要 FEISHU_APP_ID / FEISHU_APP_SECRET（或用 -app-id / -app-secret）",
            file=sys.stderr,
        )
        return 1

    holder = _ApproverHolder()

    app = bootstrap.new(
        bootstrap.Options(
            provider=bootstrap.ProviderOptions(
                kind=args.provider, model=args.model, base_url=args.base_url
            ),
            work_dir=args.workdir,
            sandbox=not args.no_sandbox,
            max_turns=args.max_turns,
            thinking=args.thinking,
            approver=holder,
            heartbeat_every=8,
        )
    )
    try:
        bot = Bot(
            FeishuConfig(
                app_id=args.app_id,
                app_secret=args.app_secret,
                listen_addr=args.addr,
                path=args.path,
                verification_token=args.verify_token,
                work_dir=app.work_dir,
                sandbox=not args.no_sandbox,
            ),
            app.engine,
            app.sessions,
        )
        # 引擎装配完成后才装得上真正的审批通道（需要 bot 发卡片）。
        holder.set(bot.approver())

        print(
            f"AgentOps 机器人已启动 | addr={args.addr} path={args.path} "
            f"workdir={app.work_dir} sandbox={not args.no_sandbox}"
        )
        await bot.start()
        return 0
    finally:
        app.close()


def main() -> None:
    try:
        code = asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n(feishu: 收到中断，正在退出)")
        code = 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层统一兜底
        print(f"feishu: {exc}", file=sys.stderr)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
