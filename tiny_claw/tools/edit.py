"""edit_file 工具：整个工具层里最"啰嗦"的一个，也是最值得啰嗦的一个。

对应专栏第 7 讲「容错艺术」。真实情况是这样的：模型给出的 old_string
几乎不可能与磁盘内容字节级一致——缩进可能差两格、空行可能多一个、
复制时可能丢了尾部换行。如果只做精确匹配，Agent 会陷入
"改了三次都没改上"的循环，白白烧掉几千 token。

所以这里做四级降级匹配，并且**把命中了哪一级回传给模型**：
让模型知道自己刚才的偏差在哪，下一轮就能自愈，而不是盲目重试。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import IntEnum

from .fs import resolve_path
from .registry import Tool


class MatchLevel(IntEnum):
    """匹配级别，数字越小越严格。"""

    EXACT = 0      # 逐字节相同
    TRIM = 1       # 忽略每行首尾空白
    NORMALIZE = 2  # 忽略所有空白差异（连续空白折叠为一个空格）
    FUZZY = 3      # 相似度阈值匹配

    def __str__(self) -> str:
        return ("exact", "trim", "normalize", "fuzzy")[self.value]


class EditError(Exception):
    """edit_file 的业务失败（对应 Go 里 applyEdit 返回的 error）。"""


def _or_empty_json(s: str) -> str:
    if s.strip() == "":
        return "{}"
    return s


@dataclass
class _EditInput:
    path: str = ""
    old_string: str = ""
    new_string: str = ""
    replace_all: bool = False


class EditFileTool(Tool):
    """对文件做精确的字符串替换（带容错降级）。"""

    def __init__(self, root: str = "", fuzzy_threshold: float = 0.0) -> None:
        self.root = root
        # fuzzy_threshold 是模糊匹配的相似度下限，越低越激进。
        # 0.82 是实测下来"能救回大部分缩进错误、又不会误伤邻居代码"的取值。
        self.fuzzy_threshold = fuzzy_threshold

    @property
    def name(self) -> str:
        # 契约第 3 节写死工具名为 edit_file；Go 源同名，无冲突。
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "对文件做精确的字符串替换。old_string 必须与文件内容足够接近"
            "（支持缩进/空白差异的容错匹配）。new_string 传空串即为删除。"
            "多处命中时必须确认 replace_all。"
        )

    @property
    def input_schema(self) -> str:
        return """{
  "type": "object",
  "properties": {
    "path": {"type": "string", "description": "目标文件绝对路径，或相对工作目录的路径"},
    "old_string": {"type": "string", "description": "要被替换的原文，需与文件内容一致（允许缩进与空白差异）"},
    "new_string": {"type": "string", "description": "替换后的新内容，传空字符串表示删除"},
    "replace_all": {"type": "boolean", "description": "是否替换所有匹配处，默认 false"}
  },
  "required": ["path", "old_string", "new_string"]
}"""

    async def execute(self, call_input: str) -> str:
        try:
            in_data = _EditInput(**json.loads(_or_empty_json(call_input)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"edit_file: invalid input: {exc}") from exc

        if in_data.old_string == "":
            raise ValueError("edit_file: old_string must not be empty")

        try:
            path = resolve_path(self.root, in_data.path)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"edit_file: {exc}") from exc

        return await asyncio.to_thread(
            self._edit_sync, path, in_data.old_string, in_data.new_string, in_data.replace_all
        )

    def _edit_sync(self, path: str, old_str: str, new_str: str, replace_all: bool) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError as exc:
            raise ValueError(f"edit_file: {exc}") from exc

        threshold = self.fuzzy_threshold or 0.82

        try:
            result, level, count = apply_edit(src, old_str, new_str, replace_all, threshold)
        except EditError as exc:
            # 错误信息里带上"我尽力到什么程度"，比干巴巴的 not found 有用得多。
            raise ValueError(f"edit_file: {exc}") from exc

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(result)
        except OSError as exc:
            raise ValueError(f"edit_file: write back: {exc}") from exc

        msg = f"edited {path}: {count} replacement(s) at {level} level"
        if level != MatchLevel.EXACT:
            msg += "（注意：非精确匹配，已按原文缩进对齐，请确认结果符合预期）"
        return msg


# ---------------------------------------------------------------------------
# 多级模糊匹配
# ---------------------------------------------------------------------------


def apply_edit(
    src: str, old_str: str, new_str: str, replace_all: bool, threshold: float
) -> "tuple[str, MatchLevel, int]":
    """依次尝试四级匹配，返回新内容、命中的级别、替换处数。

    命中多处却没要求全部替换，是典型的误伤源头，宁可让 Agent 再想一次。
    """
    src_lines, src_trailing = _split_lines(src)
    pat_lines, _ = _split_lines(old_str)
    new_lines, _ = _split_lines(new_str)

    if len(pat_lines) == 0:
        raise EditError("old_string must not be empty")
    if len(pat_lines) > len(src_lines):
        raise EditError(
            f"old_string has {len(pat_lines)} lines but file only has {len(src_lines)}"
        )

    for lvl in (MatchLevel.EXACT, MatchLevel.TRIM, MatchLevel.NORMALIZE, MatchLevel.FUZZY):
        wins = _find_windows(src_lines, pat_lines, lvl, threshold)
        if not wins:
            continue

        # 命中多处却没要求全部替换，是典型的误伤源头，宁可让 Agent 再想一次。
        if len(wins) > 1 and not replace_all:
            raise EditError(
                f"old_string matched {len(wins)} locations at {lvl} level; "
                f"narrow it down or set replace_all=true"
            )

        # 非精确匹配时必须把新内容重新对齐到原文缩进。
        # 否则模型给的新文本会按它自己以为的缩进写进去，代码格式被静默破坏
        # ——这类 bug 极难排查，因为"逻辑对了但缩进了"。
        repl = new_lines
        if lvl != MatchLevel.EXACT:
            repl = _reindent(
                new_lines, _leading_space(src_lines[wins[0][0]]), _leading_space(pat_lines[0])
            )

        out = _replace_windows(src_lines, wins, repl)
        res = "\n".join(out)
        if src_trailing:
            res += "\n"
        return res, lvl, len(wins)

    raise EditError("old_string not found after exact/trim/normalize/fuzzy matching")


def _find_windows(
    src: "list[str]", pat: "list[str]", lvl: MatchLevel, threshold: float
) -> "list[tuple[int, int]]":
    """找出所有（精确类）或最优（模糊类）的匹配窗口。"""
    n, m = len(src), len(pat)
    if m == 0 or m > n:
        return []

    if lvl == MatchLevel.FUZZY:
        # 模糊级别只在所有精确级别都失败时兜底，且只取最像的那一处，
        # 避免"处处都像"导致误改。
        best, best_score = -1, 0.0
        for start in range(0, n - m + 1):
            s = _similarity(_norm_join(src[start : start + m]), _norm_join(pat))
            if s > best_score:
                best_score, best = s, start
        if best < 0 or best_score < threshold:
            return []
        return [(best, best + m)]

    wins: "list[tuple[int, int]]" = []
    i = 0
    while i + m <= n:
        if _window_matches(src[i : i + m], pat, lvl):
            wins.append((i, i + m))
            i += m  # 不重叠，保证 replace_all 的语义可预期
        else:
            i += 1
    return wins


def _window_matches(win: "list[str]", pat: "list[str]", lvl: MatchLevel) -> bool:
    for i in range(len(pat)):
        if lvl == MatchLevel.EXACT:
            if win[i] != pat[i]:
                return False
        elif lvl == MatchLevel.TRIM:
            if win[i].strip() != pat[i].strip():
                return False
        elif lvl == MatchLevel.NORMALIZE:
            if _norm_line(win[i]) != _norm_line(pat[i]):
                return False
        else:
            return False
    return True


def _replace_windows(
    src: "list[str]", wins: "list[tuple[int, int]]", new_lines: "list[str]"
) -> "list[str]":
    out: "list[str]" = []
    prev = 0
    for a, b in wins:
        out.extend(src[prev:a])
        out.extend(new_lines)
        prev = b
    out.extend(src[prev:])
    return out


def _split_lines(s: str) -> "tuple[list[str], bool]":
    """按行切分，并记录原文是否以换行结尾（用于无损还原）。"""
    if s == "":
        return [], False
    lines = s.split("\n")
    trailing = False
    if (n := len(lines)) > 0 and lines[n - 1] == "":
        lines = lines[: n - 1]
        trailing = True
    return lines, trailing


def _leading_space(s: str) -> str:
    """取出一行开头的空白（缩进）。"""
    i = 0
    while i < len(s) and s[i] in (" ", "\t"):
        i += 1
    return s[:i]


def _reindent(new_lines: "list[str]", src_indent: str, pat_indent: str) -> "list[str]":
    """把新内容重新对齐到原文的缩进基准。

    规则：新行的缩进 = 源码行的缩进 + (新行自身的缩进 - 模式行的缩进)。
    这样既保住了原文的位置，也保留了新内容内部的相对层级关系。
    """
    out: "list[str]" = []
    for l in new_lines:
        own = _leading_space(l)
        extra = ""
        if own.startswith(pat_indent) or len(own) > len(pat_indent):
            extra = own[len(pat_indent):]
        out.append(src_indent + extra + l.lstrip(" \t"))
    return out


def _norm_line(s: str) -> str:
    """把一行内所有连续空白折叠成单个空格。

    "    if err != nil {" 与 "if err != nil {" 在这一级视为相同。
    """
    return " ".join(s.split())


def _norm_join(lines: "list[str]") -> str:
    return "\n".join(_norm_line(l) for l in lines)


def _similarity(a: str, b: str) -> float:
    """返回两个归一化文本的相似度（0~1），基于编辑距离。"""
    if a == b:
        return 1.0
    ra, rb = list(a), list(b)
    la, lb = len(ra), len(rb)
    if la == 0 or lb == 0:
        return 0.0
    d = _edit_distance(ra, rb)
    longer = la if la > lb else lb
    return 1.0 - d / longer


def _edit_distance(a: "list[str]", b: "list[str]") -> int:
    """标准 Levenshtein 距离，用两行滚动数组把空间压到 O(min(m,n))。"""
    la, lb = len(a), len(b)
    prev = list(range(lb + 1))
    cur = [0] * (lb + 1)
    for i in range(1, la + 1):
        cur[0] = i
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = _min3(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev, cur = cur, prev
    return prev[lb]


def _min3(a: int, b: int, c: int) -> int:
    m = a
    if b < m:
        m = b
    if c < m:
        m = c
    return m
