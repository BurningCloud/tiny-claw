"""spawn_subagent 工具：把复杂探索任务隔离到独立的子 Agent 里跑。

对应专栏第 17 讲。解决的问题很具体：主 Agent 为了"摸清这个项目怎么组织的"
读了 30 个文件，这 30 个文件的内容会永久占据主上下文；等它真正开始改代码时，
上下文已经被探索过程吃掉了大半。

Subagent 的做法是：开一个独立的、上下文干净的 Agent 去做探索，只把**结论**
带回主会话。主上下文的污染被限制在一小段摘要上。

Runner 由上层注入，避免 tools 包反向依赖 engine 包（会成环）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from .registry import Tool


def _or_empty_json(s: str) -> str:
    if s.strip() == "":
        return "{}"
    return s


@dataclass
class _SubagentInput:
    task: str = ""


class SubagentTool(Tool):
    """启动一个上下文隔离的子 Agent 完成探索性任务。"""

    def __init__(self, runner: "Callable[[str], Awaitable[str]] | None" = None) -> None:
        # runner 由上层注入，返回子 Agent 的最终结论文本。
        self.runner = runner

    @property
    def name(self) -> str:
        return "spawn_subagent"

    @property
    def description(self) -> str:
        return (
            "启动一个上下文隔离的子 Agent 完成探索性任务（如摸清项目结构、定位某处实现），"
            "只返回结论摘要。用于避免大量探索内容污染主会话上下文。"
        )

    @property
    def input_schema(self) -> str:
        return """{
  "type": "object",
  "properties": {
    "task": {"type": "string", "description": "交给子 Agent 的完整任务描述，要足够自包含"}
  },
  "required": ["task"]
}"""

    async def execute(self, call_input: str) -> str:
        if self.runner is None:
            raise ValueError("spawn_subagent: runner not configured")

        try:
            in_data = _SubagentInput(**json.loads(_or_empty_json(call_input)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"spawn_subagent: invalid input: {exc}") from exc

        if in_data.task.strip() == "":
            raise ValueError("spawn_subagent: task is empty")

        # 交给注入的 runner 执行（可能是引擎的 run_subagent，会自己跑一轮 ReAct）。
        return await self.runner(in_data.task)
