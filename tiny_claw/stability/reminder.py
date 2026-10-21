"""Reminder：Agent 的"缰绳"之一 —— 死循环检测与错误自愈。

对应专栏第 4 章 / 第 15 讲 / 第 14 讲。

这一整章解决的都是同一类问题：模型本身没问题，问题在于它没有"自我监控"
的能力——它不知道自己已经原地打转了十次，也不知道刚才那条命令差点删库。
这些必须由 Harness 从外部兜住。

关键原则：**用运行时机制兜，不用提示词兜**。
在系统提示里写一句"请不要陷入死循环"，效果约等于"请不要感冒"。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 仅在类型检查时依赖中立表示层，避免运行期与 schema 包产生硬耦合。
    from tiny_claw.schema.message import ToolCall


def _normalize_space(s: str) -> str:
    """折叠所有空白为无空白。模型多一个换行也应识别为"同一件事做了两次"。

    Go 的 strings.Fields 会把任意空白（含换行、制表符）切成词再拼回，
    等价于把输入里的所有空白抹平。
    """
    return "".join(s.split())


# ---------------------------------------------------------------------------
# 死循环检测（专栏第 15 讲）
# ---------------------------------------------------------------------------


class Reminder:
    """检测并打断死循环。

    检测手段很朴素：给每轮的工具调用序列算一个指纹，连续 N 次指纹相同，
    就认定它在原地打转，强行往上下文里注入打断指令。朴素但有效——
    死循环的表现形式就是"重复"。

    注：Go 版用 sync.Mutex 保护状态，因为 goroutine 是抢占式的；Python 的
    asyncio 是协作式单线程，协程之间不会在 observe() 中途被切走，因此这里
    不需要锁（context.Context 按契约整体删除，并发统一走 asyncio）。
    """

    def __init__(self, threshold: int = 3, heartbeat_every: int = 0) -> None:
        # threshold <= 0 时退化为默认 3 次，与 Go 的 NewReminder 行为一致。
        if threshold <= 0:
            threshold = 3
        self.threshold = threshold
        # HeartbeatEvery 是常规进度提醒的间隔轮数，0 表示不启用。
        self.heartbeat_every = heartbeat_every
        self._last_fp = ""
        self._streak = 0
        # cooldown 是打断后的冷却轮数，避免刚提醒完又被判定一次。
        self._cooldown = 0
        self._turn = 0

    def observe(self, calls: list[ToolCall]) -> bool:
        """记录本轮工具调用指纹。返回 True 表示应当注入打断提醒。"""
        fp = fingerprint(calls)
        self._turn += 1

        if self._cooldown > 0:
            self._cooldown -= 1
            return False

        if fp == self._last_fp:
            self._streak += 1
        else:
            self._last_fp = fp
            self._streak = 1

        if self._streak >= self.threshold:
            self._streak = 0
            # 打断后冷却一轮，给模型消化提醒的时间。
            self._cooldown = 1
            return True
        return False

    def streak(self) -> int:
        """返回当前连续重复次数（供日志与 Tracing 使用）。"""
        return self._streak

    def turn(self) -> int:
        """返回当前轮次。"""
        return self._turn

    def reset(self) -> None:
        """重置检测状态（新任务开始时调用）。"""
        self._last_fp = ""
        self._streak = 0
        self._cooldown = 0
        self._turn = 0


def build_loop_alert(turn: int) -> str:
    """生成死循环打断指令。

    措辞是刻意设计过的：不是"你做错了"，而是逼它显式回答
    "原本想达成什么 / 为什么这条路不通"。一旦被迫把意图写清楚，
    模型通常就能跳出循环——这比单纯说"换个方法"有效得多。
    """
    return (
        "## ⚠️ 运行时提醒（由 Harness 自动注入，非用户发言）\n\n"
        "你刚才连续多次发起了**完全相同**的工具调用，且结果没有进展。已判定为死循环。\n\n"
        "立刻停止重复，并按以下顺序自检：\n\n"
        "1. 先明确说出来：你原本想达成什么？上一次的返回为什么证明这条路走不通？\n"
        "2. 换一种**根本不同**的方法：换工具、换参数，或先用 read_file 读一下相关源码再动手。\n"
        "3. 如果已经尝试 3 种以上方式仍无进展，直接停下来向用户说明卡点。\n"
        "   继续空转只会烧掉上下文窗口，对解决问题没有任何帮助。\n\n"
        f"（当前第 {turn} 轮）\n"
    )


def build_heartbeat(turn: int) -> str:
    """生成常规进度提醒。

    长任务里模型容易"忘掉最初的目标"，定期提醒它能显著降低跑偏率。
    """
    return (
        f"## 运行时提醒（第 {turn} 轮）\n\n"
        "请重新确认：用户的原始目标是什么？当前进展到哪一步？\n"
        "如果已经偏离目标，立即回到主线上；如果已达成，直接给出结论收尾。\n"
    )


def fingerprint(calls: list[ToolCall]) -> str:
    """计算一轮工具调用的指纹。

    入参里的空白差异不计入：模型有时只是多了个换行，
    那仍然是"同一件事做了两次"，必须被识别为重复。
    """
    h = hashlib.md5()
    for c in calls:
        h.update(f"{c.name}|{_normalize_space(c.input)}\n".encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 错误自愈（专栏第 14 讲）
# ---------------------------------------------------------------------------


@dataclass
class Rule:
    """一条"错误特征 → 救援指南"的映射。"""

    pattern: re.Pattern
    advice: str


# DefaultRules 覆盖实践中最高频的几类工具报错。
#
# 设计要点：把"工具报错"翻译成"下一步该怎么做的具体指引"。
# 模型不是不知道报错，而是不知道报错之后该干什么。
DEFAULT_RULES: list[Rule] = [
    Rule(
        re.compile(r"(?i)no such file or directory"),
        "路径不存在。先用 bash 列目录确认真实路径（相对路径是相对工作目录解析的），或用 read_file 打开父目录。",
    ),
    Rule(
        re.compile(r"(?i)command not found|is not recognized as an internal"),
        "命令不存在或当前系统不支持。先确认该工具是否已安装、是否在 PATH 中；Windows 与 Linux 的命令集不同，注意区分。",
    ),
    Rule(
        re.compile(r"(?i)permission denied|access is denied"),
        "权限不足。不要尝试 sudo/chmod 777 绕过。确认是否真的需要写该路径，或换到工作目录内的位置。",
    ),
    Rule(
        re.compile(r"(?i)old_string not found"),
        "替换失败：old_string 与文件内容差异过大。先用 read_file 重新读取目标区域，严格按行号和内容复刻原文（含缩进），再重试 edit_file。",
    ),
    Rule(
        re.compile(r"(?i)matched \d+ locations"),
        "old_string 命中了多处，已拒绝修改以避免误伤。请补充更多上下文让它唯一，或显式设置 replace_all=true。",
    ),
    Rule(
        re.compile(r"(?i)context length|maximum context|prompt is too long"),
        "上下文超出模型窗口。请缩小范围：用 offset/limit 分段读取文件，或用 grep 过滤后再读，不要一次性读整个大文件。",
    ),
    Rule(
        re.compile(r"(?i)timed out|timeout"),
        "命令执行超时。拆分成更小的步骤，或加上 --limit/head 之类的参数减少输出量。",
    ),
    Rule(
        re.compile(r"(?i)merge conflict|CONFLICT"),
        "存在合并冲突。先 git status 看清冲突文件，用 read_file 查看冲突标记，再逐处人工决策。",
    ),
    Rule(
        re.compile(r"(?i)connection refused|dial tcp|no such host"),
        "网络或服务不可达。确认服务是否已启动、端口与地址是否正确，不要反复重试同一请求。",
    ),
]


class RecoveryAdvisor:
    """根据工具输出匹配救援指南。

    把"工具报错"翻译成"下一步该怎么做"，模型才知道报错之后该干什么。
    """

    def __init__(self, extra: list[Rule] | None = None) -> None:
        # 默认规则在前，extra 在后；命中时按出现顺序拼接，去重。
        self.rules: list[Rule] = list(DEFAULT_RULES)
        if extra:
            self.rules.extend(extra)

    def advise(self, output: str) -> str:
        """返回应当注入的救援提示，无匹配时返回空串。"""
        if not output:
            return ""

        hits: list[str] = []
        seen: set[str] = set()
        for r in self.rules:
            if r.pattern.search(output):
                if r.advice in seen:
                    continue
                seen.add(r.advice)
                hits.append(r.advice)

        if not hits:
            return ""

        lines = [
            "## 🩺 错误自愈提示（Harness 自动注入）\n\n",
            "刚才的工具调用报错了。按下面的指引调整，不要原样重试：\n\n",
        ]
        for i, h in enumerate(hits, 1):
            lines.append(f"{i}. {h}\n")
        lines.append("\n")
        return "".join(lines)
