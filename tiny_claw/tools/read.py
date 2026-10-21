"""read_file 工具：带行号的文件读取。

带行号不是装饰：模型后续要用 edit_file 定位修改点，行号是它与文件系统
之间唯一稳定的坐标系。没有行号，模型只能靠"猜"上下文，替换失败率陡增。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from .fs import resolve_path, truncate_runes
from .registry import Tool


def _or_empty_json(s: str) -> str:
    """空串按空对象处理，避免工具侧到处判空。"""
    if s.strip() == "":
        return "{}"
    return s


def _format_line(no: int, line: str) -> str:
    """用固定宽度对齐行号，方便模型肉眼比对。"""
    return _pad_right(str(no), 6) + "│ " + line + "\n"


def _pad_right(s: str, width: int) -> str:
    if len(s) >= width:
        return s
    return s + " " * (width - len(s))


@dataclass
class _ReadInput:
    path: str = ""
    offset: int = 1
    limit: int = 0


class ReadFileTool(Tool):
    """提供带行号的文件读取。"""

    def __init__(self, root: str = "", max_lines: int = 0, max_line_chars: int = 0) -> None:
        # root 限定读取根目录，为空表示不限制。
        self.root = root
        # max_lines 是单次返回的最大行数，防止大文件直接撑爆上下文。
        self.max_lines = max_lines
        # max_line_chars 单行超过该长度会被截断（压缩日志/生成代码的常见需求）。
        self.max_line_chars = max_line_chars

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "读取文件内容并返回带行号的文本。适合查看源码、日志与配置。"
            "大文件请用 offset/limit 分段读取。"
        )

    @property
    def input_schema(self) -> str:
        return """{
  "type": "object",
  "properties": {
    "path": {"type": "string", "description": "文件绝对路径，或相对工作目录的路径"},
    "offset": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
    "limit": {"type": "integer", "description": "最多读取多少行，默认全部（受 MaxLines 限制）"}
  },
  "required": ["path"]
}"""

    async def execute(self, call_input: str) -> str:
        try:
            in_data = _ReadInput(**json.loads(_or_empty_json(call_input)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"read_file: invalid input: {exc}") from exc

        try:
            path = resolve_path(self.root, in_data.path)
        except Exception as exc:  # noqa: BLE001 — 路径越界等是可预期失败
            raise ValueError(f"read_file: {exc}") from exc

        return await asyncio.to_thread(self._read_sync, path, in_data)

    def _read_sync(self, path: str, in_data: _ReadInput) -> str:
        max_lines = self.max_lines or 2000
        limit = in_data.limit or max_lines
        if limit > max_lines:
            limit = max_lines
        offset = in_data.offset
        if offset < 1:
            offset = 1
        max_chars = self.max_line_chars or 2000

        try:
            f = open(path, "r", encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ValueError(f"read_file: {exc}") from exc

        # 单行可能极长（比如 minified JS），放宽逐行读取上限。
        buf: list[str] = []
        line_no = 0
        emitted = 0
        total = 0
        with f:
            for raw in f:
                line_no += 1
                total += 1
                if line_no < offset:
                    continue
                if emitted >= limit:
                    # 不再读下去，但要知道文件还有多长，回传给模型。
                    total += sum(1 for _ in f)
                    break
                line = raw.rstrip("\n")
                if len(line) > max_chars:
                    line = truncate_runes(line, max_chars)
                buf.append(_format_line(line_no, line))
                emitted += 1

        if emitted == 0:
            return f"(file {path} has no content at offset {offset}; total {total} lines)"

        head = f"=== {path} (lines {offset}-{offset + emitted - 1} of {total}) ===\n"
        return head + "".join(buf)
