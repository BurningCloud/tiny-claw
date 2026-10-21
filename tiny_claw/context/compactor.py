"""上下文压缩（Compactor）。

对应专栏第 3 讲「上下文工程体系」。

这一层是整个 Harness 里最见功力的部分：它要解决的是「无限轮次之后，
上下文窗口迟早会爆」这个根本矛盾。核心设计是**阶梯降级**——
轻中度膨胀只做无损掩码，重度才动刀，副作用压到最低。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum

from tiny_claw.schema.message import Message, Role

# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------


def estimate_tokens(s: str) -> int:
    """本地的 token 估算。

    为什么不直接问模型要真实计数：那要额外一次 API 往返，而压缩决策在
    每一轮都要做。估算的误差远小于「为了精确而付出的延迟与成本」。
    策略：ASCII 字符约 4 个 1 token，中文按 1 字 1 token 保守估算。
    """
    if s == "":
        return 0
    ascii_count = 0
    total = 0
    for r in s:  # Python 的 str 天然按字符（rune 等价）遍历
        total += 1
        if ord(r) < 128:
            ascii_count += 1
    return ascii_count // 4 + (total - ascii_count) + 1


def message_tokens(m: Message) -> int:
    """估算单条消息的体量，包含思考内容与工具入参。"""
    n = estimate_tokens(m.content)
    if m.thinking:
        n += estimate_tokens(m.thinking)
    for tc in m.tool_calls:
        n += estimate_tokens(tc.name) + estimate_tokens(tc.input)
    return n


def messages_tokens(msgs: list[Message]) -> int:
    """估算一组消息的总量。"""
    total = 0
    for m in msgs:
        total += message_tokens(m)
    return total


# ---------------------------------------------------------------------------
# 档位与策略
# ---------------------------------------------------------------------------


class Level(IntEnum):
    """当前生效的压缩档位，用于日志与 Benchmark 归因。"""

    NONE = 0  # 未压缩
    MASK = 1  # 远期历史整体掩码
    TRUNC = 2  # 近期消息逐条截断
    HARD = 3  # 硬裁剪（保底）

    def __str__(self) -> str:
        return {
            Level.NONE: "none",
            Level.MASK: "mask",
            Level.TRUNC: "truncate",
            Level.HARD: "hard",
        }[self]


@dataclass
class Policy:
    """阶梯降级的压缩策略。

    核心思想是「分级」，而不是一刀切：轻度膨胀时只做无损的远期掩码，
    中度时截断单条消息，重度时才动刀裁剪。这样绝大多数轮次里
    模型看到的都是完整信息，压缩的副作用被压到最低。
    """

    # MaxTokens 是模型的上下文窗口预算。
    max_tokens: int
    # MaskRatio 达到该水位触发远期掩码，默认 0.70。
    mask_ratio: float = 0.70
    # TruncateRatio 达到该水位触发逐条截断，默认 0.85。
    truncate_ratio: float = 0.85
    # KeepRecent 是掩码后原样保留的最近消息条数，默认 12。
    keep_recent: int = 12
    # MaxMessageChars 是截断档位下单条消息的字符上限，默认 2400。
    max_message_chars: int = 2400


def default_policy(max_tokens: int = 120000) -> Policy:
    """给出一组实测可用的默认值。"""
    if max_tokens <= 0:
        max_tokens = 120000
    return Policy(
        max_tokens=max_tokens,
        mask_ratio=0.70,
        truncate_ratio=0.85,
        keep_recent=12,
        max_message_chars=2400,
    )


# ---------------------------------------------------------------------------
# 压缩器
# ---------------------------------------------------------------------------


class Compactor:
    """执行上下文压缩。"""

    def __init__(self, policy: Policy) -> None:
        """构造时做兜底与比例交换。

        - 比率为 0 时填回安全默认值；
        - 若 mask_ratio > truncate_ratio（调用方写反），交换二者，
          保证「掩码档位永远先于截断档位触发」这个不变量。
        """
        pol = policy
        if pol.mask_ratio <= 0:
            pol.mask_ratio = 0.70
        if pol.truncate_ratio <= 0:
            pol.truncate_ratio = 0.85
        if pol.mask_ratio > pol.truncate_ratio:
            pol.mask_ratio, pol.truncate_ratio = pol.truncate_ratio, pol.mask_ratio
        self.pol = pol

    def compact(self, msgs: list[Message]) -> tuple[list[Message], Level]:
        """按阶梯策略压缩消息序列，返回压缩结果与生效档位。"""
        limit = self.pol.max_tokens
        mask_at = int(limit * self.pol.mask_ratio)
        trunc_at = int(limit * self.pol.truncate_ratio)

        if messages_tokens(msgs) <= mask_at:
            return msgs, Level.NONE

        out = msgs
        level = Level.MASK

        out = _mask_history(out, self.pol.keep_recent)
        if messages_tokens(out) > trunc_at:
            out = _truncate_messages(out, self.pol.max_message_chars)
            level = Level.TRUNC
        if messages_tokens(out) > limit:
            out = _hard_trim(out, limit)
            level = Level.HARD
        return out, level


# ---------------------------------------------------------------------------
# 工作记忆：孤儿清理 + 配对修复
# ---------------------------------------------------------------------------


def working_memory(msgs: list[Message], keep_recent: int) -> list[Message]:
    """取出参与下一轮请求的「工作记忆」。

    这里处理的是比「超长」更隐蔽的一类 crash：孤儿工具回执。
    压缩或截断后，如果留下一条 role=tool 的消息、却没有对应的
    assistant tool_call，各家 API 都会直接返回 400，整个会话就废了。
    所以裁剪完必须做一次配对修复。
    """
    if len(msgs) == 0:
        return []

    tail = msgs
    if keep_recent > 0 and len(msgs) > keep_recent:
        tail = msgs[len(msgs) - keep_recent:]
        # 从 tool 消息开头，说明它的调用方被截掉了。
        while tail and tail[0].role == Role.TOOL:
            tail = tail[1:]

    return repair_pairs(drop_orphans(tail))


def drop_orphans(msgs: list[Message]) -> list[Message]:
    """丢弃没有对应 tool_call 的工具回执。"""
    valid = {tc.id for m in msgs for tc in m.tool_calls}
    out: list[Message] = []
    for m in msgs:
        if m.role == Role.TOOL and (m.tool_call_id == "" or m.tool_call_id not in valid):
            continue
        out.append(m)
    return out


def repair_pairs(msgs: list[Message]) -> list[Message]:
    """反向修复：assistant 声明了工具调用但回执已丢失时，
    把该调用从消息里摘掉，避免出现「有调用无结果」的非法序列。
    """
    present = {
        m.tool_call_id for m in msgs if m.role == Role.TOOL and m.tool_call_id != ""
    }

    out: list[Message] = []
    for m in msgs:
        if m.role == Role.ASSISTANT and m.tool_calls:
            kept = [tc for tc in m.tool_calls if tc.id in present]
            if not kept:
                # 纯工具调用轮次，回执全丢了，整条消息没有意义。
                if m.content == "" and m.thinking == "":
                    continue
                m = replace(m)
                m.tool_calls = []
            else:
                m = replace(m)
                m.tool_calls = kept
        out.append(m)
    return out


# ---------------------------------------------------------------------------
# 三种降级档位的实现
# ---------------------------------------------------------------------------


def _mask_history(msgs: list[Message], keep_recent: int) -> list[Message]:
    """把远期历史整体替换成一条占位说明。

    注意占位文案的措辞：明确告诉模型「不要假设自己还记得」。
    否则模型会基于被抹掉的历史继续推理，产生幻觉式的自信。
    """
    if keep_recent <= 0:
        keep_recent = 12
    if len(msgs) <= keep_recent + 2:
        return msgs

    head: list[Message] = []
    i = 0
    while i < len(msgs) and msgs[i].pinned:
        head.append(msgs[i])
        i += 1

    tail_start = len(msgs) - keep_recent
    if tail_start <= i:
        return msgs

    dropped = tail_start - i
    summary = Message(
        role=Role.USER,
        content=(
            f"[上下文压缩] 中间 {dropped} 条历史（含工具调用与回执）已被省略，"
            f"当前只保留最近 {keep_recent} 条。\n"
            "不要假设自己还记得被省略的内容。若需要回溯细节，请用 read_file / bash 重新查看。"
        ),
    )

    out: list[Message] = list(head)
    out.append(summary)
    out.extend(msgs[tail_start:])
    return out


def _truncate_messages(msgs: list[Message], max_chars: int) -> list[Message]:
    """逐条截断。

    顺序很关键：**先砍 Thinking，再砍 Content**。
    历史轮次的思考过程只对当时那一轮的决策有价值，留着纯属浪费窗口。
    """
    if max_chars <= 0:
        max_chars = 2400
    # 逐条复制（Go 的 copy 是值拷贝语义，Python 需 replace 才能隔离输入不被改写）。
    out = [replace(m) for m in msgs]
    for m in out:
        if m.pinned:
            continue
        m.thinking = ""
        if len(m.content) > max_chars:
            m.content = _head_tail(m.content, max_chars)
    return out


def _hard_trim(msgs: list[Message], limit: int) -> list[Message]:
    """保底档位：优先保留 Pinned 的头部，再从最近的消息往回填充。"""
    head: list[Message] = []
    rest: list[Message] = []
    for m in msgs:
        if m.pinned:
            head.append(m)
        else:
            rest.append(m)

    budget = limit - messages_tokens(head)
    if budget < 0:
        budget = 0

    tail: list[Message] = []
    used = 0
    for i in range(len(rest) - 1, -1, -1):
        t = message_tokens(rest[i])
        if used + t > budget and len(tail) > 0:
            break
        tail.append(rest[i])
        used += t
    tail.reverse()

    out: list[Message] = list(head)
    out.extend(tail)
    return out


def _head_tail(s: str, max_chars: int) -> str:
    """保留头尾、中间省略。Python 的 str 按字符切片等价于 Go 的 []rune。"""
    if len(s) <= max_chars:
        return s
    head = max_chars // 2
    tail = max_chars - head
    return (
        s[:head]
        + f"\n…[claw: 本条已压缩，省略 {len(s) - max_chars} 字符]…\n"
        + s[-tail:]
    )
