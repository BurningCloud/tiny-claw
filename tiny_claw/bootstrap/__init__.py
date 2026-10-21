"""bootstrap：装配层。

把 Provider、工具、上下文、稳定性、可观测性拼成一个可运行的 Harness。
CLI、Benchmark、飞书机器人三个入口共用这一层。
"""

from __future__ import annotations

from tiny_claw.bootstrap.builder import (
    App,
    Options,
    ProviderOptions,
    new,
    new_provider,
)

__all__ = ["new", "new_provider", "Options", "ProviderOptions", "App"]
