"""engine：Harness 的心脏——手写 ReAct 主循环。

对应专栏第 2、3 讲。整个引擎只有一件事在做：

    组装上下文 → 问模型 → 有工具调用就执行 → 把结果喂回去 → 再问

"工业级"和"Demo 级"的分界线全在这个循环的细节里：上下文什么时候压缩、
工具失败怎么回喂、怎样发现自己已经原地打转、什么时候该认输停下来。
这些全部由 Harness 决定，模型只负责思考。
"""

from __future__ import annotations

from tiny_claw.engine.loop import Config, Engine, EngineError, Progress, Result

__all__ = ["Engine", "Config", "Progress", "Result", "EngineError"]
