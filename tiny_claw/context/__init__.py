"""上下文工程层（context）。

本包负责 Agent 的「上下文」全周期管理，是 go-tiny-claw 里最见功力的部分：

- compactor：阶梯降级压缩（mask → truncate → hard）、孤儿工具回执清理、
  调用-回执配对修复、token 估算；
- session：会话物理隔离、历史快照语义；
- memory：PLAN.md / TODO.md 外部化存储；
- composer：每轮动态重建系统提示、AGENTS.md / 技能加载。

所有对外名字均严格遵循 CONTRACT.md 第 4 节，此处统一 re-export。
"""

from __future__ import annotations

from tiny_claw.context.composer import DEFAULT_BASE_PROMPT, Composer, Skill, load_skills
from tiny_claw.context.compactor import (
    Compactor,
    Level,
    Policy,
    default_policy,
    estimate_tokens,
    message_tokens,
    messages_tokens,
    working_memory,
)
from tiny_claw.context.memory import FileMemory
from tiny_claw.context.session import (
    Manager,
    Session,
    session_id_for_chat,
    session_id_for_dir,
)

__all__ = [
    # compactor
    "Level",
    "Policy",
    "default_policy",
    "Compactor",
    "working_memory",
    "estimate_tokens",
    "message_tokens",
    "messages_tokens",
    # session
    "Session",
    "Manager",
    "session_id_for_dir",
    "session_id_for_chat",
    # memory
    "FileMemory",
    # composer
    "DEFAULT_BASE_PROMPT",
    "Skill",
    "Composer",
    "load_skills",
]
