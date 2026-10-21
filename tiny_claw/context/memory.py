"""外部化记忆（FileMemory）。

对应专栏第 13 讲，这是 OpenClaw 最值得抄的一个设计：

抛弃在内存里维护复杂状态机，强制 Agent 把规划写进 PLAN.md、
把进度写进 TODO.md。收益是三重的：

  1. 记忆不再是黑盒——你随时可以打开、阅读、甚至**手动改**；
  2. 进程重启后状态还在，长程任务可以断点续传；
  3. 这些文件天然可被压缩/重建，不占用宝贵的上下文窗口。
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def _clip(s: str, max_chars: int) -> str:
    """按字符上限裁剪文本（Python str 按字符切片，等价于 Go 的 []rune）。"""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n…（已截断）"


class FileMemory:
    """把 Agent 的「记忆与待办」外部化为本地 Markdown 文件。

    Dir 是记忆文件目录，默认 ".claw"。
    """

    def __init__(self, directory: str) -> None:
        self.directory: str = directory if directory else ".claw"

    def ensure(self) -> None:
        """创建记忆目录（失败直接抛异常，而非返回 error）。"""
        os.makedirs(self.directory, 0o755, exist_ok=True)

    def plan_path(self) -> str:
        """PLAN.md 的完整路径。"""
        return os.path.join(self.directory, "PLAN.md")

    def todo_path(self) -> str:
        """TODO.md 的完整路径。"""
        return os.path.join(self.directory, "TODO.md")

    def load_plan(self) -> str:
        """读取 PLAN.md，不存在时返回空串（不是错误）。"""
        try:
            with open(self.plan_path(), "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def save_plan(self, content: str) -> None:
        """覆写 PLAN.md（失败抛异常）。"""
        self.ensure()
        with open(self.plan_path(), "w", encoding="utf-8") as f:
            f.write(content)

    def load_todo(self) -> str:
        """读取 TODO.md，不存在时返回空串。"""
        try:
            with open(self.todo_path(), "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def save_todo(self, content: str) -> None:
        """覆写 TODO.md（失败抛异常）。"""
        self.ensure()
        with open(self.todo_path(), "w", encoding="utf-8") as f:
            f.write(content)

    def append_note(self, name: str, note: str) -> None:
        """往任意记忆文件追加一条带时间戳的记录。"""
        self.ensure()
        path = os.path.join(self.directory, name)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n- {ts} {note}\n")

    def summary(self, max_chars: int) -> str:
        """生成注入系统提示的记忆摘要。

        两侧都做长度限制：记忆文件本身也可能失控膨胀。
        """
        if max_chars <= 0:
            max_chars = 4000
        plan = self.load_plan()
        todo = self.load_todo()
        if plan == "" and todo == "":
            return ""

        parts: list[str] = []
        if plan != "":
            parts.append("## 当前计划（PLAN.md）\n\n")
            parts.append(_clip(plan, max_chars // 2))
            parts.append("\n\n")
        if todo != "":
            parts.append("## 当前待办（TODO.md）\n\n")
            parts.append(_clip(todo, max_chars // 2))
            parts.append("\n")
        return "".join(parts)
