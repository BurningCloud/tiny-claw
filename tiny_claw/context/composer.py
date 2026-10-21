"""动态系统提示组装（Composer）。

对应专栏第 10 讲。为什么不能把提示词写死成一个大字符串：
系统提示是「每轮都在变」的——项目规范随目录变、记忆随执行进度变、
运行时提醒随死循环检测结果变。写死就意味着这些能力全部失效。
"""

from __future__ import annotations

import os
import platform
import sys
import time
from dataclasses import dataclass

from tiny_claw.context.memory import FileMemory

# DefaultBasePrompt 是 go-tiny-claw 的「人格与纪律」。
#
# 这一段不是随便写的鸡汤。逐条对应 Harness 要兜住的工程问题：
# 先把规划落到文件（状态外部化）、改错先读再改（避免盲改）、
# 连续失败要换路子（死循环自愈）、不确定就问（人机协作）。
DEFAULT_BASE_PROMPT = """你是一个运行在终端里的自主编程 Agent（代号 go-tiny-claw）。

工作纪律：
1. 先理解，再动手。接到任务先用 read_file / bash 摸清现状，不要凭猜测改代码。
2. 重要任务先把规划写进 PLAN.md、把步骤写进 TODO.md，再开始执行。
   这让状态可被人类审阅，也让你在长程任务里不会跑偏。
3. 修改代码优先用 edit_file 做局部替换，避免整文件重写引入无关 diff。
4. 工具调用失败时，先读错误信息定位原因，再换一种方式重试。
   不要用完全相同的参数连续重试同一件事，那只会浪费上下文。
5. 并行独立：一轮里多个互不依赖的读取/命令，请在同一个回合里一次性发起。
6. 探索性任务（摸清项目结构、定位某处实现）优先交给 spawn_subagent，
   避免大量中间内容污染你自己的上下文。
7. 遇到破坏性操作（删除、强制推送、改系统配置），先停下来向人类确认。
8. 收尾时给出简洁结论：改了什么、为什么这么改、还有什么风险。不要复述过程。"""


@dataclass
class Skill:
    """一个外挂技能。"""

    name: str
    description: str
    body: str
    path: str


class Composer:
    """动态组装系统提示。"""

    def __init__(
        self,
        work_dir: str = "",
        base_prompt: str = "",
        agents_file: str = "",
        skill_dir: str = "",
        active_skills: list[str] | None = None,
        memory: FileMemory | None = None,
        max_section_chars: int = 0,
    ) -> None:
        # BasePrompt 是基础人格，为空时用 DefaultBasePrompt。
        self.base_prompt: str = base_prompt
        # WorkDir 是当前工作目录，也是 AGENTS.md 向上查找的起点。
        self.work_dir: str = work_dir
        # AgentsFile 是项目规范文件名，默认 AGENTS.md。
        self.agents_file: str = agents_file
        # SkillDir 是技能目录，默认 .claw/skills（相对 WorkDir）。
        self.skill_dir: str = skill_dir
        # ActiveSkills 指定要展开正文的技能名，为空则只列目录。
        self.active_skills: list[str] = active_skills or []
        # Memory 提供 PLAN.md / TODO.md 摘要。
        self.memory: FileMemory | None = memory
        # MaxSectionChars 限制每个注入段落的长度，防止单个文件吃掉整个窗口。
        self.max_section_chars: int = max_section_chars

    def compose(self, *sections: str) -> str:
        """组装完整的系统提示。sections 是运行时追加的段落
        （如死循环提醒、错误自愈指南）。
        """
        parts: list[str] = []

        base = self.base_prompt
        if base == "":
            base = DEFAULT_BASE_PROMPT
        parts.append(base)
        parts.append("\n\n")

        parts.append("## 运行环境\n\n")
        wd = self.work_dir
        if wd == "":
            wd = os.getcwd()
        parts.append(f"- 操作系统: {sys.platform}/{platform.machine()}\n")
        parts.append(f"- 工作目录: {wd}\n")
        parts.append(f"- 当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        if agents := self._load_agents():
            parts.append("## 项目规范（AGENTS.md）\n\n")
            parts.append(_clip(agents, self._limit()))
            parts.append("\n\n")

        if skills := self._skills_section():
            parts.append(skills)

        if self.memory is not None:
            if s := self.memory.summary(self._limit()):
                parts.append("## 长期记忆\n\n")
                parts.append(s)
                parts.append("\n\n")

        for s in sections:
            if s.strip() != "":
                parts.append(s)
                parts.append("\n\n")

        return "".join(parts).rstrip("\n")

    def _limit(self) -> int:
        if self.max_section_chars <= 0:
            return 8000
        return self.max_section_chars

    def _load_agents(self) -> str:
        """从工作目录向上查找项目规范文件。

        向上查找是必要的：Agent 常常在子目录里被唤起，而规范在项目根。
        """
        name = self.agents_file
        if name == "":
            name = "AGENTS.md"
        dir_ = self.work_dir
        if dir_ == "":
            dir_ = os.getcwd()
        for _ in range(8):
            try:
                with open(os.path.join(dir_, name), "r", encoding="utf-8") as f:
                    return f.read().strip()
            except FileNotFoundError:
                parent = os.path.dirname(dir_)
                if parent == dir_:
                    break
                dir_ = parent
        return ""

    def _skills_section(self) -> str:
        dir_ = self.skill_dir
        if dir_ == "":
            dir_ = ".claw/skills"
        if not os.path.isabs(dir_) and self.work_dir != "":
            dir_ = os.path.join(self.work_dir, dir_)
        try:
            skills = load_skills(dir_)
        except OSError:
            return ""
        if not skills:
            return ""

        skills.sort(key=lambda s: s.name)

        lines: list[str] = ["## 可用技能（Skills）\n\n"]
        for s in skills:
            lines.append(f"- **{s.name}**: {s.description}\n")

        active = set(self.active_skills)
        if active:
            lines.append("\n已激活技能详情：\n\n")
            for s in skills:
                if s.name in active:
                    lines.append(f"### {s.name}\n\n")
                    lines.append(_clip(s.body, self._limit()))
                    lines.append("\n\n")
        return "".join(lines)


def load_skills(directory: str) -> list[Skill]:
    """扫描目录里的 Markdown 技能文件。

    技能是「按需展开」的：平时只在提示词里挂一行名字+描述（几十个 token），
    模型判断用得上时才把正文拉进来。这与「塞几十个工具描述」是相反的取舍。
    """
    try:
        entries = os.scandir(directory)
    except OSError:
        raise
    out: list[Skill] = []
    with entries:
        for e in entries:
            if e.is_dir() or not e.name.lower().endswith(".md"):
                continue
            try:
                skill = _parse_skill(e.path)
            except OSError:
                continue
            out.append(skill)
    return out


def _parse_skill(path: str) -> Skill:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    name = os.path.splitext(os.path.basename(path))[0]
    desc = ""
    body = raw

    # 支持 --- frontmatter ---
    if raw.startswith("---"):
        rest = raw[3:]
        end = rest.find("\n---")
        if end >= 0:
            front = rest[:end]
            body = rest[end + 4:].strip()
            for line in front.split("\n"):
                k, v, ok = _cut_kv(line.strip())
                if not ok:
                    continue
                if k.lower() == "name":
                    if v != "":
                        name = v
                elif k.lower() == "description":
                    desc = v
    if desc == "":
        desc = _first_paragraph(body)
    return Skill(name=name, description=desc, path=path, body=body)


def _cut_kv(line: str) -> tuple[str, str, bool]:
    i = line.find(":")
    if i < 0:
        return "", "", False
    return line[:i].strip(), line[i + 1:].strip(), True


def _first_paragraph(s: str) -> str:
    for line in s.split("\n"):
        t = line.strip()
        if t == "" or t.startswith("#") or t.startswith("---"):
            continue
        if len(t) > 120:
            return t[:120] + "…"
        return t
    return ""


def _clip(s: str, max_chars: int) -> str:
    """按字符上限裁剪文本（Python str 按字符切片，等价于 Go 的 []rune）。"""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n…（已截断）"
