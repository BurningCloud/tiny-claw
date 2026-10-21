"""文件系统原语：沙箱路径解析与输出截断。

这一层只关心"物理边界"和"别把海量输出灌进上下文"。
真正的工具（read/write/edit/bash）都依赖这里的 resolve_path 把模型给出的
路径钉死在工作区内，以及 truncate_runes 在返回命令输出前掐头去尾。
"""

from __future__ import annotations

import os


class ResolveError(Exception):
    """路径解析失败的总类（对应 Go 的 errNoPath / errEscape 父类）。"""


class NoPathError(ResolveError):
    """未提供路径（对应 Go 的 errNoPath）。"""


class EscapeError(ResolveError):
    """路径试图逃出工作区根目录（对应 Go 的 errEscape）。"""


def resolve_path(root: str, p: str) -> str:
    """把模型给出的路径解析为本地绝对路径。

    这是 Harness 的第一道物理边界。模型（乃至提示词注入的攻击载荷）
    不该有能力读到工作目录之外的东西，比如 ~/.ssh/id_rsa 或 /etc/shadow。
    root 为空时不限制，仅用于本地可信场景。
    """
    if p.strip() == "":
        raise NoPathError("path is required")

    if not os.path.isabs(p):
        base = root
        if base == "":
            base = os.getcwd()
        p = os.path.join(base, p)
    p = os.path.normpath(p)

    if root != "":
        abs_root = os.path.abspath(root)
        abs_p = os.path.abspath(p)
        try:
            rel = os.path.relpath(abs_p, abs_root)
        except ValueError:
            # 不同盘符（Windows）等情况，relpath 会报错，直接判越界。
            raise EscapeError("path escapes the workspace root")
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            raise EscapeError("path escapes the workspace root")
    return p


def truncate_runes(s: str, max_chars: int) -> str:
    """掐头去尾式截断。

    为什么不是简单截前 N 个字符：命令输出里最有用的信息往往在两头
    （开头的错误类型、结尾的堆栈摘要），中间才是噪音。
    按 rune（Python 的 str 已是 Unicode 码点序列）切，不会切断 UTF-8 中文。
    """
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
        + "".join(runes[len(runes) - tail :])
    )
