# 接口契约（Go → Python 移植）

**这是多人并行翻译的唯一真相来源。所有人必须严格遵守，不得自行改名。**

源文件根目录（Go）：`C:\Users\ply\WorkBuddy\2026-10-19-21-40-24\go-tiny-claw`
输出根目录（Python）：`D:\pythonProject\tiny-claw`

移植的是一个 Agent Harness 教学项目（go-tiny-claw，ReAct 引擎）。
**注释是项目价值的一半**——Go 源码里那些解释"为什么这么设计"的长注释，
必须译成中文 docstring / 行内注释保留下来，不要翻译成干巴巴的接口说明。

---

## 全局约定

| 项 | 约定 |
|---|---|
| Python 版本 | 3.13（类型注解、match 语句、`X \| None` 直接用） |
| 第三方依赖 | **零**。只用标准库（含 `asyncio`、`urllib.request`、`http.server`、`subprocess`、`json`） |
| 并发模型 | Go 的 goroutine → `asyncio`。阻塞 IO 用 `asyncio.to_thread` 包起来 |
| `context.Context` | 直接删掉，不做等价物。取消靠 `asyncio.CancelledError` |
| `time.Duration` | → `duration_ms: int`（毫秒整数）。`time.Now()` → `time.time()` |
| Go 的 error 返回值 | → Python 异常。可预期的业务失败用自定义异常类 |
| Go 的 nil 降级 | → `None` 降级（如 `Tracer(None)` 就是 no-op） |
| 命名 | 文件/方法/变量 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE` |
| 类型注解 | 全部函数都要有。文件头写 `from __future__ import annotations` |
| 注释语言 | 中文 |
| 异常处理 | 工具执行这类"失败也要继续"的场景，学 Go 的 `Execution.err` 模式：把异常存进结果对象，而不是直接抛给调用方 |

---

## 1. `tiny_claw/schema/message.py`（由总负责人产出，其他人直接 import）

```python
class Role(str, Enum):
    SYSTEM = "system"; USER = "user"; ASSISTANT = "assistant"; TOOL = "tool"

@dataclass
class ToolCall:
    id: str
    name: str
    input: str = ""          # 原始 JSON 字符串，不做中间层 marshal/unmarshal
    def decode(self) -> dict[str, Any]: ...   # 空串按 {} 处理

@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: str        # JSON Schema 文本

@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    def total(self) -> int: ...

@dataclass
class Message:
    role: Role
    content: str = ""
    thinking: str = ""                                 # 慢思考，独立于 content
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""
    is_error: bool = False
    created_at: float = field(default_factory=time.time)
    pinned: bool = False                               # 压缩时永不丢弃

def new_user(text: str) -> Message: ...                # pinned=True
def new_assistant(text: str) -> Message: ...
def new_tool_result(call_id: str, name: str, content: str, is_error: bool) -> Message: ...

STOP_TOOL_USE = "tool_use"; STOP_END = "end_turn"; STOP_MAX_TOK = "max_tokens"
```

---

## 2. `tiny_claw/provider/base.py`

```python
@dataclass
class ChatRequest:
    system: str = ""
    messages: list[Message] = field(default_factory=list)
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int = 8192
    temperature: float = 0.0
    thinking: bool = False
    thinking_budget: int = 0

@dataclass
class ChatResponse:
    content: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""
    latency_ms: int = 0
    model: str = ""          # 真实命中的模型名（成本审计需要）

class EmptyResponseError(Exception): ...
class ContextTooLongError(Exception): ...

class Provider(ABC):
    @property
    def name(self) -> str: ...           # "claude" / "openai"
    @abstractmethod
    async def chat(self, req: ChatRequest) -> ChatResponse: ...
```

其余：`tiny_claw/provider/http.py`（共享 HTTP + SSE 解析 + 重试）、
`claude.py`（`ClaudeConfig`、`Claude`）、`openai.py`（`OpenAIConfig`、`OpenAI`）。

---

## 3. `tiny_claw/tools/registry.py`

```python
class Tool(ABC):
    @property
    def name(self) -> str: ...           # 必须与 ToolSpec.name 完全一致
    @property
    def description(self) -> str: ...
    @property
    def input_schema(self) -> str: ...   # JSON Schema 文本
    @abstractmethod
    async def execute(self, call_input: str) -> str: ...   # 入参是原始 JSON 字符串

# 钩子：不调用 next 就等于短路（拦截）
Hook = Callable[[Tool, str, Callable[[str], Awaitable[str]]], Awaitable[str]]

@dataclass
class Execution:
    tool_call_id: str
    name: str
    output: str = ""
    err: BaseException | None = None
    blocked: bool = False
    duration_ms: int = 0
    def is_error(self) -> bool: ...      # err is not None or blocked
    def result(self) -> str: ...         # blocked→output; err→"tool X failed: ..."; 空→"(no output)"

class Registry:
    def register(self, tool: Tool) -> None: ...     # 同名覆盖
    def use(self, hook: Hook) -> None: ...          # 先注册的在最外层
    def get(self, name: str) -> Tool | None: ...
    def all(self) -> dict[str, Tool]: ...           # 快照
    def specs(self) -> list[ToolSpec]: ...          # 按 name 排序，保证请求稳定
    async def execute(self, call: ToolCall) -> Execution: ...        # 走完整钩子链
    async def execute_all(self, calls: list[ToolCall]) -> list[Execution]: ...  # 并发但**按序回填**
```

工具实现放在 `tiny_claw/tools/` 下：`fs.py`（沙箱路径解析）、`read.py`、`write.py`、
`edit.py`、`bash.py`、`subagent.py`。
工具名常量：`read_file` / `write_file` / `edit_file` / `run_command` / `spawn_subagent`（以 Go 源为准）。

---

## 4. `tiny_claw/context/`

```python
# compactor.py
class Level(IntEnum):
    NONE = 0; MASK = 1; TRUNC = 2; HARD = 3
    def __str__(self) -> str: ...   # "none"/"mask"/"truncate"/"hard"

@dataclass
class Policy:
    max_tokens: int
    mask_ratio: float = 0.70
    truncate_ratio: float = 0.85
    keep_recent: int = 12
    max_message_chars: int = 2400

def default_policy(max_tokens: int = 120000) -> Policy: ...

class Compactor:
    def __init__(self, policy: Policy) -> None: ...     # 兜底 + 交换倒置的 ratio
    def compact(self, msgs: list[Message]) -> tuple[list[Message], Level]: ...

def working_memory(msgs: list[Message], keep_recent: int) -> list[Message]: ...
def estimate_tokens(s: str) -> int: ...
def message_tokens(m: Message) -> int: ...
def messages_tokens(msgs: list[Message]) -> int: ...

# session.py
class Session:
    def __init__(self, id: str, work_dir: str, label: str) -> None
    id: str; work_dir: str; label: str
    def append(self, *msgs: Message) -> None: ...
    def history(self) -> list[Message]: ...          # 返回快照
    def set_history(self, msgs: list[Message]) -> None: ...
    def bump_turn(self) -> int: ...
    def reset(self) -> None: ...

class Manager:
    def get(self, id: str) -> Session | None: ...
    def get_or_create(self, id: str, work_dir: str, label: str) -> Session: ...
    def delete(self, id: str) -> None: ...
    def list(self) -> list[str]: ...

def session_id_for_dir(path: str) -> str: ...
def session_id_for_chat(platform: str, chat_id: str) -> str: ...

# memory.py
class FileMemory:
    def __init__(self, directory: str) -> None
    def ensure(self) -> None: ...
    def plan_path(self) -> str: ...
    def todo_path(self) -> str: ...
    def load_plan(self) -> str: ...
    def save_plan(self, content: str) -> None: ...
    def load_todo(self) -> str: ...
    def save_todo(self, content: str) -> None: ...
    def append_note(self, name: str, note: str) -> None: ...
    def summary(self, max_chars: int) -> str: ...

# composer.py
DEFAULT_BASE_PROMPT: str      # 内容原样取自 Go 源码
@dataclass
class Skill: name, description, body, path
class Composer:
    def __init__(self, work_dir: str = "", ...) -> None
    def compose(self, *sections: str) -> str: ...
def load_skills(directory: str) -> list[Skill]: ...
```

---

## 5. `tiny_claw/stability/`

```python
# reminder.py
class Reminder:
    def __init__(self, threshold: int = 3) -> None
    def observe(self, calls: list[ToolCall]) -> bool: ...   # 连续重复到阈值→True
    def streak(self) -> int: ...
    def turn(self) -> int: ...
    def reset(self) -> None: ...

def build_loop_alert(turn: int) -> str: ...
def build_heartbeat(turn: int) -> str: ...
def fingerprint(calls: list[ToolCall]) -> str: ...

@dataclass
class Rule: pattern, advice ...
DEFAULT_RULES: list[Rule]
class RecoveryAdvisor:
    def __init__(self, extra: list[Rule] | None = None) -> None
    def advise(self, output: str) -> str: ...

# middleware.py
class RiskLevel(IntEnum): NONE, LOW, ASK, DENY   # 以 Go 源码为准
@dataclass
class Decision: level: RiskLevel, reason: str, rule: str
class Classifier(ABC): def classify(self, tool_name: str, call_input: str) -> Decision
class RegexClassifier(Classifier): ...
def default_classifier() -> RegexClassifier: ...
class BlockedError(Exception):
    def __init__(self, message: str, ...)
class ApprovalRequest: ...
class Approver(ABC): async def approve(self, req: ApprovalRequest) -> bool
class StaticApprover(Approver): ...      # 构造传 bool
class FuncApprover(Approver): ...
class TerminalApprover(Approver): ...
class Guard:
    def __init__(self, classifier: Classifier, approver: Approver) -> None
    async def hook(self, tool: Tool, call_input: str,
                   nxt: Callable[[str], Awaitable[str]]) -> str: ...
    def grant(self, tool_name: str, call_input: str) -> None: ...
```

`Guard.hook` 必须能直接 `registry.use(guard.hook)` 注册进钩子链。

---

## 6. `tiny_claw/observability/`

```python
# tracing.py
class EventKind(str, Enum):
    SESSION_START = "session_start"; USER_INPUT = "user_input"; MODEL = "model_call"
    THINKING = "thinking"; TOOL = "tool_call"; BLOCK = "blocked"
    COMPACT = "compaction"; REMINDER = "reminder"; ERROR = "error"; FINAL = "final"

@dataclass
class Event:
    session: str; turn: int; kind: EventKind; detail: str
    duration_ms: int = 0; ok: bool = True
    tokens_in: int = 0; tokens_out: int = 0; ts: float = field(default_factory=time.time)

class Tracer:
    def __init__(self, directory: str | None = None) -> None: ...  # None ⇒ no-op
    def record(self, ev: Event) -> None: ...
    def events(self) -> list[Event]: ...
    def close(self) -> None: ...
    def path(self) -> str | None: ...
    def render(self, max_events: int = 40) -> str: ...

def new_noop_tracer() -> Tracer: ...

# cost.py
@dataclass
class Pricing: input_per_1m: float; output_per_1m: float      # 字段名以 Go 源码为准
DEFAULT_PRICING: dict[str, Pricing]
@dataclass
class ModelCost: ...
@dataclass
class Report: ...
class Accountant:
    def __init__(self, pricing: dict[str, Pricing] | None = None) -> None
    def set_pricing(self, model: str, p: Pricing) -> None: ...
    def record(self, model: str, usage: Usage, latency_ms: int) -> None: ...
    def snapshot(self) -> Report: ...
```
`Report` 要有 `__str__`，输出人类可读的成本报告。

---

## 7. `tiny_claw/im/feishu.py`

```python
FEISHU_BASE = "https://open.feishu.cn"
@dataclass
class FeishuConfig: ...
class Bot:
    def __init__(self, cfg: FeishuConfig, engine: "engine.Engine", sessions: "context.Manager") -> None
    async def start(self) -> None: ...
    def approver(self) -> Approver: ...
    async def send_text(...)/send_approval_card(...) 等
```
用标准库 `http.server` 起 webhook 服务（`ThreadingHTTPServer` + 事件循环桥接）。

---

## 8. `tiny_claw/bootstrap/builder.py`

```python
@dataclass
class ProviderOptions: kind: str = "claude"; model: str = ""; base_url: str = ""
def new_provider(o: ProviderOptions) -> Provider: ...     # 读 ANTHROPIC_API_KEY / OPENAI_API_KEY
@dataclass
class Options: provider, work_dir, sandbox, max_turns, context_window,
               thinking, heartbeat_every, approver, yolo, on_progress, claw_dir
class App:
    engine: Engine; registry: Registry; memory: FileMemory
    tracer: Tracer; accountant: Accountant; sessions: Manager
    work_dir: str; claw_dir: str
    def close(self) -> None
    def session(self, id: str, label: str) -> Session
    def cli_session(self) -> Session
def new(opts: Options) -> App: ...
```

---

## 9. `tiny_claw/engine/loop.py`（总负责人产出）

```python
@dataclass
class Progress: turn, thinking, content, tool_calls, executions, compaction
@dataclass
class Result: text, turns, usage, duration_ms, exceeded
@dataclass
class Config: provider, registry, composer, compactor, memory, tracer,
              accountant, advisor, work_dir, max_turns, max_tokens,
              temperature, thinking, thinking_budget, keep_recent,
              loop_threshold, heartbeat_every, on_progress
class Engine:
    def __init__(self, cfg: Config) -> None     # 补齐所有默认值
    async def run(self, sess: Session, user_input: str) -> Result
    async def run_subagent(self, task: str) -> str
    def tracer(self) -> Tracer
    def accountant(self) -> Accountant
```

---

## 10. 入口（总负责人产出）

`tiny_claw/apps/claw.py`、`apps/bench.py`、`apps/tasks.py`、`apps/feishu_bot.py`，
用 `argparse` 对齐 Go 版 `flag` 的命令行参数，`python -m tiny_claw.apps.claw` 运行。

---

## 交付自检（每个人交付前必须跑通）

```bash
cd /d/pythonProject/tiny-claw
C:/Users/ply/.workbuddy/binaries/python/versions/3.13.12/python.exe -m compileall -q tiny_claw/<你的包>
C:/Users/ply/.workbuddy/binaries/python/versions/3.13.12/python.exe -c "import tiny_claw.<你的模块>"
```
