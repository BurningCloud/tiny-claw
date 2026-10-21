"""middleware：Agent 的"刹车系统" —— 风险分级与人工审批通道。

对应专栏第 16 讲。这里解决的问题是：模型可以调用任何工具，但有些操作
（删库、fork bomb、强制推送）一旦执行就是不可逆的事故。Harness 必须在
工具真正落地之前，把这类操作拦下来。

设计上的灵魂一句话：**Deny 级永远拦得住，Yolo（全自动）模式也只自动放行
Ask 级，绝不碰 Deny**；而"人工审批"这件事用同一套 Approver 接口就能横跨
终端、飞书、CI 三种场景——这就是"防御纵深"能复用的原因。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    # Tool 由另一个人翻译 tiny_claw.tools 包，这里只做类型标注，运行期靠
    # 鸭子类型访问 tool.name，不依赖其具体实现。
    from tiny_claw.tools import Tool


def _normalize_space(s: str) -> str:
    """折叠所有空白为无空白（与 reminder 中的同源逻辑保持一致）。"""
    return "".join(s.split())


def _first_line(s: str, max_chars: int) -> str:
    """取首行并截断到 max_chars，用于审批提示里展示命令摘要。"""
    s = s.strip()
    nl = s.find("\n")
    if nl >= 0:
        s = s[:nl]
    if len(s) > max_chars:
        s = s[:max_chars] + "…"
    return s


class RiskLevel(IntEnum):
    """操作的风险等级。

    注意：NONE 在语义上等价于 Go 版的 RiskAllow（直接放行）；LOW 是契约预留
    等级，当前规则集不会产出它。ASK = 挂起等人类审批，DENY = 硬性拒绝。
    """

    NONE = 0  # 直接放行
    LOW = 1  # 低风险（预留，当前规则未产出）
    ASK = 2  # 挂起并等待人类审批
    DENY = 3  # 直接拒绝，人类也批不了


@dataclass
class Decision:
    """一次风险判定结果。

    rule 字段是契约新增的审计字段（Go 版只有 Level/Reason），这里把命中的
    正则原文记进去，方便 Tracing 与日志回溯是哪条规则拦的。
    """

    level: RiskLevel
    reason: str = ""
    rule: str = ""


class Classifier(ABC):
    """判定一次工具调用的风险等级。

    classify 的 call_input 是原始的 JSON 入参字符串（与 Go 一致，不在中间层
    反复 marshal/unmarshal）。
    """

    @abstractmethod
    def classify(self, tool_name: str, call_input: str) -> Decision:
        raise NotImplementedError


@dataclass
class _Pattern:
    """一条正则规则（内部使用）。re 是编译后的正则，reason 是给人看的原因。"""

    re: re.Pattern
    reason: str


class RegexClassifier(Classifier):
    """基于规则的默认分类器。

    为什么不用模型来判断"这危不危险"：安全边界必须是**确定性的**。
    让一个会幻觉的系统去守最后一道防线，本身就是设计事故。
    """

    def __init__(self) -> None:
        self.deny: list[_Pattern] = []
        self.ask: list[_Pattern] = []

    def classify(self, tool_name: str, call_input: str) -> Decision:
        cmd = _command_of(tool_name, call_input)
        if cmd.strip() == "":
            return Decision(level=RiskLevel.NONE)

        for p in self.deny:
            if p.re.search(cmd):
                return Decision(level=RiskLevel.DENY, reason=p.reason, rule=p.re.pattern)
        for p in self.ask:
            if p.re.search(cmd):
                return Decision(level=RiskLevel.ASK, reason=p.reason, rule=p.re.pattern)
        return Decision(level=RiskLevel.NONE)


def _command_of(tool_name: str, call_input: str) -> str:
    """从工具入参里取出真正要执行的命令/内容。

    只有把命令又包了一层 JSON（如 {"command": "..."}）的情况才需要解析；
    其余情况直接返回原始入参。工具名以 Go 源为准，只认 "bash"。
    """
    if tool_name == "bash":
        try:
            data = json.loads(call_input) if call_input else {}
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            return str(data.get("command", ""))
    return call_input


# Deny 与 Ask 的分工：
#   - Deny 是"无论如何都不该发生"的（删库、fork bomb、把网络脚本喂给 shell）；
#   - Ask 是"业务上合法但影响面大"的（强制推送、装依赖、重启容器）。
_DENY_RULES: list[tuple[str, str]] = [
    (r"(?i)\brm\s+(-[rRfF]+\s+)*(/|/\*|~|~/|\.|\./\*|\*|$)", "递归删除根目录或家目录"),
    (r"(?i)\brm\s+-[rRfF]+.*\s+(/etc|/usr|/bin|/sbin|/var|/System|C:\\Windows|C:\\Users)\b", "删除系统目录"),
    (r"(?i):\(\)\s*\{.*:\|:&\s*\};:", "fork 炸弹"),
    (r"(?i)\bmkfs(\.[a-z0-9]+)?\b", "格式化文件系统"),
    (r"(?i)\bdd\s+.*\bof=/dev/[sh]d", "直接写块设备"),
    (r"(?i)\b(shutdown|reboot|init\s+0|halt)\b", "关机/重启"),
    (r"(?i)>\s*/dev/(sd|nvme|hd)", "覆写块设备"),
    (r"(?i)\bchmod\s+(-R\s+)?777\s+/", "对根目录开放 777 权限"),
    (r"(?i)(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b", "把网络内容直接喂给 shell"),
    (r"(?i)\bgit\s+push\s+[^\n]*--force(-with-lease)?\s*(origin\s+)?(main|master)\b", "强制推送到主分支"),
    (r"(?i)\bgit\s+clean\s+-[fx]", "git clean -f/-x 会删除未跟踪文件"),
    (r"(?i)\bgit\s+reset\s+--hard\b", "git reset --hard 会丢弃未提交改动"),
    (r"(?i)\b(rd|rmdir)\s+/s\s+/q", "Windows 静默递归删除"),
    (r"(?i)\bdel\s+/[sfq]+\s+(C:\\|\*|%)", "Windows 强制删除"),
    (r"(?i)\bformat\s+[a-zA-Z]:", "格式化磁盘"),
]

_ASK_RULES: list[tuple[str, str]] = [
    (r"(?i)\bgit\s+(push|commit|merge|rebase|cherry-pick)\b", "会改变仓库历史或远端状态"),
    (r"(?i)\bgit\s+checkout\s+[^\n]*--\s", "会丢弃工作区改动"),
    (r"(?i)\b(rm|mv)\s+-?(r|rf)?\s+\S+", "删除或移动文件"),
    (r"(?i)\b(npm|pnpm|yarn|go|pip|uv|apt|brew)\s+(install|add|remove|uninstall|get)\b", "会改动依赖环境"),
    (r"(?i)\bdocker\s+(rm|rmi|stop|system\s+prune)\b", "会影响容器/镜像"),
    (r"(?i)\bkubectl\s+(delete|apply|scale|rollout|drain)\b", "会影响 K8s 集群状态"),
    (r"(?i)\bsudo\s+", "提权执行"),
    (r"(?i)\bcurl\s+-X\s*(POST|PUT|DELETE|PATCH)", "会产生副作用的外部请求"),
    (r"(?i)\bsystemctl\s+(restart|stop|reload)\b", "会影响系统服务"),
]


def default_classifier() -> RegexClassifier:
    """给出一组开箱可用的规则（见上方 _DENY_RULES / _ASK_RULES）。"""
    c = RegexClassifier()
    for expr, reason in _DENY_RULES:
        c.deny.append(_Pattern(re=re.compile(expr), reason=reason))
    for expr, reason in _ASK_RULES:
        c.ask.append(_Pattern(re=re.compile(expr), reason=reason))
    return c


class BlockedError(Exception):
    """操作被安全中间件拦截。

    它不是"工具执行失败"，而是一种**结果**——模型需要看到它并改变策略。
    引擎通过检查异常类型把它识别成"拦截"而不是"工具崩溃"。
    """

    def __init__(self, message: str, tool: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.tool = tool

    def __str__(self) -> str:
        return self.message


@dataclass
class ApprovalRequest:
    """发给人类的一次审批请求。"""

    session_id: str = ""
    tool_name: str = ""
    input: str = ""
    reason: str = ""


class Approver(ABC):
    """人工审批通道。

    同一个接口可以有多种实现：终端里敲 y/N、飞书里点卡片、
    或者 CI 环境下直接拒绝。这正是"防御纵深"能跨场景复用的原因。

    契约把 Go 的 (ctx, req) -> (bool, error) 收敛为 approve(req) -> bool：
    context 已整体删除（取消靠 asyncio.CancelledError），异常也统一通过
    抛出来表达（见 Guard 的处理）。
    """

    @abstractmethod
    async def approve(self, req: ApprovalRequest) -> bool:
        raise NotImplementedError


class StaticApprover(Approver):
    """恒定的审批器，用于测试或非交互场景（构造时传入是否放行）。"""

    def __init__(self, allow: bool) -> None:
        self.allow = allow

    async def approve(self, req: ApprovalRequest) -> bool:
        return self.allow


class FuncApprover(Approver):
    """把任意函数包装成 Approver，便于接入飞书卡片回调等异步来源。

    fn 的签名是 (ApprovalRequest) -> bool | Awaitable[bool]；
    既支持同步函数也支持异步函数（协程）。
    """

    def __init__(self, fn: Callable[[ApprovalRequest], bool | Awaitable[bool]]) -> None:
        self._fn = fn

    async def approve(self, req: ApprovalRequest) -> bool:
        result = self._fn(req)
        if asyncio.iscoroutine(result):
            return await result
        return result


class TerminalApprover(Approver):
    """在终端向人类提问。

    Windows 下 input() 会阻塞事件循环，因此必须用 asyncio.to_thread 包裹，
    并加一层 asyncio.wait_for 超时（默认 5 分钟，对应 Go 版的 5*time.Minute）。
    """

    def __init__(self, timeout: float = 300.0) -> None:
        self.timeout = timeout

    async def approve(self, req: ApprovalRequest) -> bool:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._prompt, req),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            return False

    @staticmethod
    def _prompt(req: ApprovalRequest) -> bool:
        cmd = _command_of(req.tool_name, req.input)
        print()
        print("⚠️  高危操作待审批")
        print(f"   工具: {req.tool_name}")
        if cmd:
            print(f"   内容: {_first_line(cmd, 200)}")
        print(f"   判定: {req.reason}")
        if req.session_id:
            print(f"   会话: {req.session_id}")
        ans = input("\n允许执行? [y/N] ").strip().lower()
        return ans in ("y", "yes")


class Guard:
    """挂在工具执行链上的安全中间件（专栏第 16 讲）。

    设计要点：
    - Deny 级永远拦得住，Yolo（全自动）模式也只自动放行 Ask 级，绝不碰 Deny。
    - granted 缓存本会话内已批准过的操作，避免同一条命令反复打扰人类。
    - 没有审批通道时默认拒绝，而不是放行。
    """

    def __init__(
        self,
        classifier: Classifier | None = None,
        approver: Approver | None = None,
    ) -> None:
        # Go 版 NewGuard 的 nil 兜底：分类器默认用 default_classifier()，
        # 审批器默认用 StaticApprover(False)——没有审批通道时拒绝而非放行。
        self.classifier = classifier or default_classifier()
        self.approver = approver or StaticApprover(False)
        # granted 缓存本会话内已批准过的操作指纹。
        self.granted: dict[str, bool] = {}
        # on_decision 是可选的审计回调，Tracing 可以接上去。
        self.on_decision: Callable[[str, Decision, bool], None] | None = None

    async def hook(
        self,
        tool: Tool,
        call_input: str,
        nxt: Callable[[str], Awaitable[str]],
    ) -> str:
        """实现 tools.Hook，可直接 registry.use(guard.hook) 注册进钩子链。

        不调用 nxt 就等于短路（拦截）。被拦截时**抛 BlockedError**，
        这样引擎才能通过检查异常类型把它识别成"拦截"而不是"工具崩溃"。
        """
        name = tool.name
        d = self.classifier.classify(name, call_input)

        if d.level == RiskLevel.NONE:
            return await nxt(call_input)

        if d.level == RiskLevel.DENY:
            self._notify(name, d, False)
            raise BlockedError(
                tool=name,
                message=(
                    f"⛔ 已拒绝执行 {name}：{d.reason}。\n"
                    "该操作被 Harness 安全策略硬性禁止，不可绕过。"
                    "请改用其它方案，并向用户说明原因。"
                ),
            )

        # RiskLevel.ASK：走人工审批通道。
        key = _fingerprint_of(name, call_input)
        if self.granted.get(key):
            # 后来批准的豁免：本会话内已批准过，直接放行。
            return await nxt(call_input)

        approved = await self._request_approval(name, call_input, d)
        if not approved:
            raise BlockedError(
                tool=name,
                message=(
                    f"⛔ {name} 已被拒绝：{d.reason}。\n"
                    "不要换一种写法重试同一件事，先向用户确认意图。"
                ),
            )
        self.granted[key] = True
        self._notify(name, d, True)
        return await nxt(call_input)

    async def _request_approval(self, name: str, call_input: str, d: Decision) -> bool:
        try:
            return await self.approver.approve(
                ApprovalRequest(
                    tool_name=name,
                    input=call_input,
                    reason=d.reason,
                )
            )
        except Exception as exc:  # 审批通道异常 → 视为拒绝，并转为 BlockedError
            self._notify(name, d, False)
            raise BlockedError(
                tool=name,
                message=f"⛔ {name} 未获批准（审批通道异常: {exc}）。",
            ) from exc

    def grant(self, tool_name: str, call_input: str) -> None:
        """预先授权某个操作（比如用户在启动时就说了"本次全自动"）。"""
        self.granted[_fingerprint_of(tool_name, call_input)] = True

    def _notify(self, tool: str, d: Decision, approved: bool) -> None:
        if self.on_decision is not None:
            self.on_decision(tool, d, approved)


def _fingerprint_of(name: str, call_input: str) -> str:
    """对"工具名 + 归一化入参"取 md5，作为放行豁免的缓存键。"""
    return hashlib.md5(
        (name + "|" + _normalize_space(call_input)).encode("utf-8")
    ).hexdigest()
