# 🐾 tiny-claw（Python 版）

从零手写的 Agent Harness，是 Go 项目 `go-tiny-claw` 的完整 Python 移植。

**只用 Python 标准库**（`asyncio` / `urllib.request` / `http.server` / `subprocess`），
零第三方依赖。装好 Python 就能跑，不需要 `pip install` 任何东西。

```
组装上下文 → 问模型 → 有工具调用就执行 → 把结果喂回去 → 再问
              ↑                                          │
              └──────────────────────────────────────────┘
```

---

## 为什么不用框架

| 痛点 | 传统框架 | Harness 的做法 |
|---|---|---|
| 上下文失控 | 塞几十个工具，模型降智 | 只用 4 个原语，从源头消灭膨胀 |
| 状态失控 | 状态封在黑盒里，死循环你改不了 | 记忆写进 PLAN.md / TODO.md，记事本就能改 |
| 边界失控 | 想拦删库？自己写正则累死 | 安全中间件，高危操作一键挂起等人审批 |

---

## 架构

```
                        ┌──────────────────────────┐
   用户输入 ───────────▶│      engine.ReAct 循环    │
                        └────────────┬─────────────┘
                                     │
        ┌────────────────┬───────────┼───────────┬──────────────┐
        ▼                ▼           ▼           ▼              ▼
   context.Composer  provider    tools      stability     observability
   （动态提示词）    （大脑接入）  （四原语）  （刹车系统）   （链路+成本）
        │                            │           │
   context.Compactor           Registry 钩子链   Guard
   （阶梯压缩）                （拦截/审批）    （分级+人工审批）
        │
   context.Session / FileMemory
   （会话隔离 / PLAN.md·TODO.md）
```

---

## 目录结构与章节映射

| 路径 | 对应章节 | 关键点 |
|---|---|---|
| `tiny_claw/schema/message.py` | 01 架构演进 | 与厂商无关的中立消息抽象；`thinking` 独立成字段 |
| `tiny_claw/engine/loop.py` | 02 核心心脏 | 手写 ReAct 主循环，全部工程细节的决策点 |
| `tiny_claw/engine/loop.py` | 03 慢思考 | 独立 Reasoning 阶段，可单独丢弃/压缩 |
| `tiny_claw/provider/` | 04 大脑接入 | Provider 接口 + Claude / OpenAI 双实现 |
| `tiny_claw/tools/registry.py` | 05 Tool Registry | 注册、路由、**并发执行且按序回填** |
| `tiny_claw/tools/bash.py` | 06 大道至简 | 四原语哲学：给个 shell，模型自己跑 git/grep/make |
| `tiny_claw/tools/edit.py` | 07 容错艺术 | 四级降级模糊匹配（精确→去空白→归一化→相似度） |
| `tiny_claw/tools/registry.py` | 08 并发提效 | `execute_all` 一轮内并行多个独立 tool_call |
| `tiny_claw/im/feishu.py` | 09 飞书集成 | 事件流、按群隔离会话、卡片审批 |
| `tiny_claw/context/composer.py` | 10 提示词组装 | 动态加载 AGENTS.md 与外挂 Skills |
| `tiny_claw/context/session.py` | 11 会话管理 | Session 物理隔离，一个群一条上下文 |
| `tiny_claw/context/compactor.py` | 12 阶梯压缩 | 掩码 → 截断 → 硬裁剪，三级降级 |
| `tiny_claw/context/memory.py` | 13 记忆沉淀 | 状态外部化为 PLAN.md / TODO.md |
| `tiny_claw/stability/reminder.py` | 14 错误自愈 | 报错特征 → 具体救援指引 |
| `tiny_claw/stability/reminder.py` | 15 死循环干预 | 调用指纹 + 连续计数，强制注入打断指令 |
| `tiny_claw/stability/middleware.py` | 16 防御纵深 | 危险命令拦截 + 人工审批（YOLO + 纵深） |
| `tiny_claw/tools/subagent.py` | 17 任务委派 | 子 Agent 上下文隔离，只带结论回主会话 |
| `tiny_claw/observability/cost.py` | 18 成本追踪 | Harness 层拦截 token 消耗，按模型聚合 |
| `tiny_claw/observability/tracing.py` | 19 链路追踪 | JSONL 落盘，失败路径可完整复盘 |
| `tiny_claw/apps/bench.py` | 20 科学度量 | 自动化评估脚本，关键词断言 + 成本/轮次统计 |
| `tiny_claw/apps/claw.py` | 21 实战一 | 完整 CLI 引擎，多轮探索与修 Bug |
| `tiny_claw/apps/feishu_bot.py` | 22 实战二 | AgentOps 助手，日志分析与故障修复审批 |

---

## 快速开始

```bash
# Claude
export ANTHROPIC_API_KEY=sk-...

# 或 OpenAI 兼容端点（DeepSeek / 通义 / vLLM / Ollama）
export OPENAI_API_KEY=sk-...

python -m tiny_claw.apps.claw
python -m tiny_claw.apps.claw -prompt "找出 loop.py 里的并发 bug 并修掉"
python -m tiny_claw.apps.claw -provider openai -model deepseek-chat \
    -base-url https://api.deepseek.com

python -m tiny_claw.apps.bench -workdir ./
```

交互模式下的命令：`/help` `/reset` `/cost` `/trace` `/plan` `/exit`。

也可以安装成可执行命令：

```bash
pip install -e .
claw -prompt "..."
```

---

## 与 Go 版的对应关系

分层一一对应，方便对照阅读：

| Go | Python |
|---|---|
| `internal/engine/loop.go` | `tiny_claw/engine/loop.py` |
| `internal/provider/` | `tiny_claw/provider/` |
| `internal/tools/` | `tiny_claw/tools/` |
| `internal/context/` | `tiny_claw/context/` |
| `internal/stability/` | `tiny_claw/stability/` |
| `internal/observability/` | `tiny_claw/observability/` |
| `internal/im/feishu.go` | `tiny_claw/im/feishu.py` |
| `internal/bootstrap/` | `tiny_claw/bootstrap/` |
| `cmd/{claw,bench,feishu}/` | `tiny_claw/apps/{claw,bench,feishu_bot}.py` |

**关键映射规则**

| Go | Python |
|---|---|
| goroutine | `asyncio`；阻塞 IO 用 `asyncio.to_thread` |
| `context.Context` | 删除，取消靠 `asyncio.CancelledError` |
| `time.Duration` | `duration_ms: int` |
| `error` 返回值 | 异常；但工具执行失败按 Go 的 `Execution.err` 模式存进结果对象 |
| `interface` | `ABC` + `@abstractmethod` |
| `net/http` | `http.server.ThreadingHTTPServer` |
| `encoding/json` / HTTP 客户端 | `json` / `urllib.request` |

**两处刻意的偏差（已超越 Go 源的一致性）**

1. **工具名**：Go 源码里 bash 工具叫 `"bash"`，本项目统一以 Go 源为准使用 `"bash"`。
2. **空回复处理**：Go 版 Claude 实现返回 `(resp, ErrEmptyResponse)`，而 OpenAI 实现
   有一处返回 `(None, ErrEmptyResponse)`，会让引擎走进 "nil response" 分支直接报错退出——
   这与接口注释声明的意图（"注入提醒并重试，而不是直接崩掉"）相矛盾。
   Python 版统一按声明的意图处理：视为一次空回复，注入提醒后重试。

---

## 验证

```bash
python -m unittest discover -s tests -t . -v
```

| 测试 | 覆盖的风险 |
|---|---|
| `tests/test_tools_edit.py` | 四级降级匹配、缩进重对齐、多处命中的拒绝、路径逃逸 |
| `tests/test_context_compactor.py` | 孤儿工具回执清理、调用/回执配对修复、压缩触发 |
| `tests/test_stability_reminder.py` | 死循环判定计数、Deny/Ask/Allow 规则表、错误自愈匹配 |
| `tests/test_engine_loop.py` | 用桩 Provider 跑通完整 ReAct 循环、并发按序回填、轮次上限兜底 |

---

## 几个值得单独看的设计

### 1. 多级模糊匹配（`tools/edit.py`）

模型给的 `old_string` 经常对不上原文（缩进、空行、全半角）。直接报错会让模型反复失败，
于是降级：精确 → 折叠空白 → 归一化 → 相似度阈值。命中时**重对齐缩进**，否则会静默破坏代码格式。

### 2. 先砍 Thinking，再砍 Content（`context/compactor.py`）

慢思考是过程，不是结论。上下文紧张时先丢它，正文能多留好几轮。
这也是 `thinking` 必须独立成字段的原因。

### 3. 孤儿工具回执（`context/compactor.py: working_memory`）

压缩后可能只剩 assistant 的 tool_call 而没有对应回执——部分厂商 API 会直接 400。
`working_memory` 负责清理孤儿回执、修复调用/回执配对。

### 4. YOLO + 防御纵深（`stability/middleware.py`）

`-yolo` 只自动放行 **Ask 级**（装依赖、git push）；
**Deny 级**（`rm -rf /`、fork bomb、`curl|sh`、强制推主分支）由分类器独立拦住，
与审批通道无关，永远拦得住。

### 5. 死循环干预（`stability/reminder.py`）

对工具调用序列算指纹，连续 N 次完全相同就注入打断指令。
这是运行时机制，不靠提示词祈祷模型自觉。

### 6. 状态外部化（`context/memory.py`）

PLAN.md / TODO.md 落在磁盘上。死循环了、上下文炸了，人类拿记事本就能改。

---

## 安全模型

三层：

1. **文件系统沙箱**——工具只能访问工作目录，跨目录拒绝（`-no-sandbox` 可关）。
2. **规则拦截**——Deny 级永不放行，Ask 级停下来等人。
3. **人工审批**——终端敲 y/N，或飞书点卡片。

---

## 扩展点

| 想加什么 | 改哪里 |
|---|---|
| 新工具 | 继承 `tools.Tool`，然后 `registry.register(...)` |
| 新模型厂商 | 实现 `provider.Provider` |
| 新的审批通道 | 实现 `stability.Approver`（已有终端 / 飞书卡片 / 静态） |
| 新的拦截规则 | 扩展 `stability.default_classifier()` 的规则表 |
| 新的错误自愈 | 往 `stability.DEFAULT_RULES` 追加一条 `Rule` |
| 新的注入源 | 改 `context.Composer.compose()` |

---

## 产物

```
tiny_claw/
├── schema/          中立消息抽象
├── provider/        Claude / OpenAI + 共享 HTTP·SSE
├── tools/           四原语 + Registry + 钩子链
├── context/         Composer / Compactor / Session / Memory
├── stability/       Reminder / Guard（拦截 + 审批）
├── observability/   Tracing（JSONL） / Cost
├── im/              飞书渠道
├── bootstrap/       装配层
├── engine/          ReAct 主循环
└── apps/            claw / bench / feishu_bot
tests/
```

运行时产物落在工作目录的 `.claw/` 下：`trace.jsonl`、`PLAN.md`、`TODO.md`。

---

## 说明

- 需要 Python **3.10+**（`CONTRACT.md` 里按 3.13 编写，实际兼容 3.10 起）。
- 内置价目表会过期，真实使用请用 `Accountant.set_pricing()` 注入最新价格。
- `CONTRACT.md` 是这次移植的接口契约文档，记录了跨层 API 的约定与冲突裁断，值得一看。
