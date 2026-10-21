"""bash 工具：终极 YOLO 原语。

OpenClaw 最反直觉也最有效的设计：与其给模型准备几十个专用工具，
不如只给一个 shell。有了 bash，模型自己就能跑 git、grep、make、curl，
甚至自己写临时脚本解决问题——工具描述占用的上下文几乎归零，
而能力边界反而被极大扩展。

代价是安全风险，这部分由 stability 包的中间件兜住（危险命令拦截 +
人工审批），而不是靠在提示词里写"请不要执行危险命令"。
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass

from .registry import Tool


class BashError(Exception):
    """bash 执行的"业务失败"（命令超时 / 非零退出）。

    Go 的 Execute 返回 (output, error) 两个值——即便命令失败，已经抓到的
    部分输出也要回给模型当"观察结果"。Python 的 execute 只能返回 str 或抛异常，
    于是把部分输出塞进 output 属性，让 Registry 在捕获异常时把它回填进
    Execution.output（虽然 Execution.result() 在 err!=nil 时优先用错误信息，
    但 Tracing 仍可看到这部分输出）。
    """

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


def _or_empty_json(s: str) -> str:
    if s.strip() == "":
        return "{}"
    return s


@dataclass
class _BashInput:
    command: str = ""
    timeout_ms: int = 0


class BashTool(Tool):
    """在工作目录执行 shell 命令并返回合并后的 stdout/stderr。"""

    def __init__(self, work_dir: str = "", default_timeout: int = 0, max_output_chars: int = 0) -> None:
        # work_dir 是命令执行的工作目录。
        self.work_dir = work_dir
        # default_timeout 默认超时（毫秒），防止一条卡死的命令挂住整个 ReAct 循环。
        self.default_timeout = default_timeout
        # max_output_chars 输出截断上限。
        self.max_output_chars = max_output_chars

    @property
    def name(self) -> str:
        """工具名。

        CONTRACT.md 第 3 节的工具名清单备注「以 Go 源为准」，而 Go 源码里
        BashTool.Name() 返回的正是 "bash"，故这里统一用 "bash"。
        """
        return "bash"

    @property
    def description(self) -> str:
        return (
            "在工作目录执行 shell 命令并返回合并后的 stdout/stderr。"
            "适合运行测试、git 操作、grep 搜索、构建等。禁止用于任何破坏性操作。"
        )

    @property
    def input_schema(self) -> str:
        return """{
  "type": "object",
  "properties": {
    "command": {"type": "string", "description": "要执行的 shell 命令"},
    "timeout_ms": {"type": "integer", "description": "超时毫秒数，默认 120000"}
  },
  "required": ["command"]
}"""

    async def execute(self, call_input: str) -> str:
        try:
            in_data = _BashInput(**json.loads(_or_empty_json(call_input)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"bash: invalid input: {exc}") from exc

        if in_data.command.strip() == "":
            raise ValueError("bash: command is empty")

        timeout = self.default_timeout or 120000
        if in_data.timeout_ms > 0:
            timeout = in_data.timeout_ms

        # Windows 没有 /bin/sh，退回 cmd.exe。
        if sys.platform == "win32":
            prog, flag = "cmd", "/C"
        else:
            prog, flag = "sh", "-c"

        try:
            proc = await asyncio.create_subprocess_exec(
                prog, flag, in_data.command,
                cwd=self.work_dir or None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise ValueError(f"bash: cannot start shell: {exc}") from exc

        # 合并 stdout/stderr：模型既要看结果也要看报错，而且分开捕获会丢失
        # 两者的相对顺序，排查时非常误导。
        try:
            out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout / 1000)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            out = (out_bytes or b"").decode("utf-8", errors="replace")
            max_chars = self.max_output_chars or 16000
            text = _truncate(out, max_chars)
            b = f"$ {in_data.command}\n"
            if text:
                b += text
                if not text.endswith("\n"):
                    b += "\n"
            b += f"\n[claw: 命令超时（{timeout}ms）已终止]"
            raise BashError(f"bash: command timed out after {timeout}ms", output=b)

        max_chars = self.max_output_chars or 16000
        text = _truncate(out_bytes.decode("utf-8", errors="replace"), max_chars)

        b = f"$ {in_data.command}\n"
        if text:
            b += text
            if not text.endswith("\n"):
                b += "\n"

        if proc.returncode not in (0, None):
            # 命令失败是"观察结果"而非"系统故障"：内容照样回给模型，
            # 但用 error 标记，让引擎知道这一轮工具执行出了问题。
            b += f"\n[exit: code {proc.returncode}]"
            raise BashError(f"bash: exit code {proc.returncode}", output=b)

        if not text:
            b += "(no output)\n"
        return b


def _truncate(s: str, max_chars: int) -> str:
    """掐头去尾式截断，与 fs.truncate_runes 同语义（避免循环 import，这里就近实现）。"""
    if max_chars <= 0:
        return s
    runes = list(s)
    if len(runes) <= max_chars:
        return s
    head = max_chars // 2
    tail = max_chars - head
    return (
        "".join(runes[:head])
            + f"\n...[claw: 已截断 {len(runes) - max_chars} 字符]...\n"
            + "".join(runes[len(runes) - tail:])
        )
