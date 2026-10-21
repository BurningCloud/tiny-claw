"""tiny-claw：从零手写的 Agent Harness（Python 移植版）。

这是 Go 项目 go-tiny-claw 的 Python 移植：一个可以真跑的 ReAct 引擎，
用来讲清楚"Agent 框架里那些不性感但决定成败的部分"——
上下文怎么压、死循环怎么发现、工具失败怎么回喂、什么时候该认输。

设计上刻意保持与 Go 版的分层一一对应，方便对照阅读：

    engine        ReAct 主循环（心脏）
    provider      大脑接入层，屏蔽厂商 API 差异
    tools         极简四原语 + 钩子链（拦截 / 审批）
    context       上下文工程：动态提示词、阶梯压缩、会话隔离
    stability     稳定性：死循环检测、错误自愈、高危拦截
    observability 链路追踪 + 成本审计
    im            渠道接入（飞书）
    bootstrap     装配层，三个入口共用

**零第三方依赖**：只用 Python 标准库（asyncio / urllib / http.server /
subprocess）。装好 Python 3.11+ 就能跑，不需要 pip install 任何东西。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
