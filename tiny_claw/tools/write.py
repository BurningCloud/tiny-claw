"""write_file 工具：整文件写入。

设计取向（OpenClaw 的四原语哲学）：只提供"整文件覆盖写"，不提供追加、
不提供局部插入。局部修改一律走 edit_file。工具越少，模型的选择成本越低，
出错面也越小。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

from .fs import resolve_path
from .registry import Tool


def _or_empty_json(s: str) -> str:
    if s.strip() == "":
        return "{}"
    return s


@dataclass
class _WriteInput:
    path: str = ""
    content: str = ""


class WriteFileTool(Tool):
    """整文件写入工具。"""

    def __init__(self, root: str = "", max_bytes: int = 0) -> None:
        self.root = root
        # max_bytes 是单文件写入上限，防止模型一次吐出几百兆内容。
        self.max_bytes = max_bytes

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "将完整内容写入文件（覆盖写）。会按需创建父目录。"
            "修改已有文件请优先使用 edit_file。"
        )

    @property
    def input_schema(self) -> str:
        return """{
  "type": "object",
  "properties": {
    "path": {"type": "string", "description": "目标文件绝对路径，或相对工作目录的路径"},
    "content": {"type": "string", "description": "要写入的完整文件内容"}
  },
  "required": ["path", "content"]
}"""

    async def execute(self, call_input: str) -> str:
        try:
            in_data = _WriteInput(**json.loads(_or_empty_json(call_input)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"write_file: invalid input: {exc}") from exc

        try:
            path = resolve_path(self.root, in_data.path)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"write_file: {exc}") from exc

        return await asyncio.to_thread(self._write_sync, path, in_data.content)

    def _write_sync(self, path: str, content: str) -> str:
        max_bytes = self.max_bytes or (2 << 20)  # 2MB
        if len(content) > max_bytes:
            raise ValueError(
                f"write_file: content too large ({len(content)} bytes > {max_bytes})"
            )

        if parent := os.path.dirname(path):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"write_file: mkdir {parent}: {exc}") from exc

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            raise ValueError(f"write_file: {exc}") from exc

        lines = 1 + content.count("\n")
        return f"wrote {path} ({len(content)} bytes, {lines} lines)"
