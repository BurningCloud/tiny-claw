"""工具与执行层。

对应专栏第 2 章「极简工具与物理交互」与第 16 讲「防御纵深」。

这一层只暴露极少数原语——read_file / write_file / edit_file / bash /
spawn_subagent（工具名与 Go 源保持一致）——而不是为每个业务动作定义工具。
理由很直接：工具描述本身要占用上下文窗口，塞给模型几十个工具描述等于亲手
把模型的注意力稀释掉。给模型一个 shell，它能自己跑 git、grep、make，
工具层面的上下文膨胀从源头就被消灭了。

并发靠 asyncio（工具内部阻塞 IO 用 asyncio.to_thread，bash 用
asyncio.create_subprocess_exec），Registry.execute_all 用 asyncio.gather 并发并按序回填；
横切逻辑（危险命令拦截、人工审批、Tracing）挂载在钩子链上，先注册的最外层，
支持 registry.use(guard.hook) 直接注册一个 async 函数。
"""

from __future__ import annotations

from .bash import BashError, BashTool
from .edit import EditError, EditFileTool, MatchLevel, apply_edit
from .fs import EscapeError, NoPathError, ResolveError, resolve_path, truncate_runes
from .read import ReadFileTool
from .registry import Execution, Hook, Registry, Tool
from .subagent import SubagentTool
from .write import WriteFileTool

__all__ = [
    "Tool",
    "Hook",
    "Execution",
    "Registry",
    "resolve_path",
    "ResolveError",
    "NoPathError",
    "EscapeError",
    "truncate_runes",
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "apply_edit",
    "MatchLevel",
    "EditError",
    "BashTool",
    "BashError",
    "SubagentTool",
]
