"""飞书渠道层。

对外导出 CONTRACT.md 第 7 节约定的公共符号。
"""

from __future__ import annotations

from tiny_claw.im.feishu import FEISHU_BASE, Bot, FeishuConfig, FeishuError

__all__ = ["FEISHU_BASE", "Bot", "FeishuConfig", "FeishuError"]
