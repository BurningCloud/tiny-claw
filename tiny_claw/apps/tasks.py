"""评估任务集。

Task.expect 是"最终回答里必须出现的关键词"。这是最朴素也最实用的判分方式：
严格断言会脆弱到没法维护，人工判分又没法自动化。
关键词断言刚好落在中间——它抓得住"答非所问"这类真问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Task:
    """一个评估任务。"""

    name: str
    prompt: str
    expect: list[str] = field(default_factory=list)


def default_tasks() -> list[Task]:
    """与本项目配套的默认任务集。

    建议：把这里改成你自己项目的任务。Benchmark 的价值不在于跑得多，
    而在于它测的是你真正在乎的那件事。
    """
    return [
        Task(
            name="模块信息",
            prompt="读取当前项目的 pyproject.toml，回答两点：项目名是什么？要求的 Python 版本是什么？"
            "只给这两个事实，不要解释。",
            expect=["tiny-claw"],
        ),
        Task(
            name="文件定位",
            prompt="用 grep 定位 apply_edit 这个函数定义在哪，回答格式必须是「文件路径:行号」，"
            "不要输出其它内容。",
            expect=["edit.py"],
        ),
        Task(
            name="结构理解",
            prompt="列出 tiny_claw 目录下的所有子包名，并用一句话说明每个包负责什么。不要粘贴代码。",
            expect=["engine", "tools", "context"],
        ),
        Task(
            name="代码统计",
            prompt="统计 tiny_claw 目录下一共有多少个 .py 文件。只回答一个整数，不要任何解释。",
            expect=[],  # 只验证能正常收尾，不校验具体数值
        ),
        Task(
            name="一次性并发",
            prompt="在同一个回合里一次性读取 tiny_claw/engine/loop.py 和 tiny_claw/tools/registry.py 两个文件，"
            "然后各用一句话概括它们的职责。注意：必须一次发起两个读取。",
            expect=["loop.py", "registry.py"],
        ),
        Task(
            name="状态外部化",
            prompt="把「给 tiny-claw 增加 HTTP 健康检查接口」的实施计划写进 PLAN.md，"
            "并把拆解出的步骤写进 TODO.md，然后告诉我你写了几步。",
            expect=["PLAN.md", "TODO.md"],
        ),
    ]
