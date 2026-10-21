"""飞书渠道层：把 Harness 接进真实世界（企业 IM）。

对应专栏第 9 讲（接入飞书事件流）与第 22 讲（AgentOps 小助手）。

接 IM 之后会立刻暴露两个在 CLI 里不存在的问题：
  1. 并发会话：A 群让 Agent 重构代码，B 群让它查日志，必须互不干扰；
  2. 人工审批要有真实通道：终端里敲 y/N 在服务器上没人看得到，
     审批必须变成一张能被点按的卡片。

设计取舍（与 Go 的差异，均来自 CONTRACT.md 的全局约定）：
  * `net/http` 起服务 → 标准库 `http.server.ThreadingHTTPServer`；
  * `context.Context` 整层删除；Go 里靠 ctx 传递的"当前群 ID"改用
    `contextvars.ContextVar` 承载——它是 asyncio 世界里 ctx value 的等价物，
    不污染任何函数签名，Approver 的接口依旧是 `async def approve(self, req)`；
  * `time.Duration` → `duration_ms: int`；
  * HTTP 客户端用 `urllib.request`（零第三方依赖），阻塞调用包进 `asyncio.to_thread`；
  * 方法名按契约第 7 节改成 snake_case（start / approver / send_text / send_approval_card）。
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 这些都是"只用于类型注解"的跨层符号，运行时一律不 import，
    # 避免 feishu 包加载就强依赖尚未产出的 engine / context / stability。
    from tiny_claw.context.session import Manager, Session, session_id_for_chat
    from tiny_claw.engine.loop import Engine, Result
    from tiny_claw.stability import ApprovalRequest, Approver

# 飞书开放平台根地址（契约写死，一个字不许改）。
FEISHU_BASE = "https://open.feishu.cn"

# 当前任务所属的群 ID。替代 Go 的 WithChat/ChatFrom（ctx 已被删除）。
#
# 为什么走 ContextVar 而不是给引擎加参数：Approver 的接口签名是固定的，
# 而"这次操作属于哪个群"显然是一个随请求变化的值。ContextVar 正是 asyncio
# 世界里为此而生的东西——它被绑定到具体协程，互不串台。
_CHAT_ID: contextvars.ContextVar[str] = contextvars.ContextVar("feishu_chat_id", default="")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class FeishuError(Exception):
    """飞书渠道层的可预期业务错误。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class FeishuConfig:
    """飞书机器人的配置。"""

    app_id: str = ""
    app_secret: str = ""
    # listen_addr 是回调服务监听地址，如 ":8080" 或 "0.0.0.0:8080"。
    listen_addr: str = ":8080"
    # path 是事件回调路径，如 "/webhook"。
    path: str = "/webhook"
    # verification_token 用于校验事件来源（v1 事件）。
    verification_token: str = ""
    # work_dir 是 Agent 的工作目录。
    work_dir: str = ""
    # sandbox 限制工具的文件访问范围到 work_dir。
    sandbox: bool = False
    # approval_timeout_ms 是等待人类点卡片的时长（毫秒）。
    approval_timeout_ms: int = 5 * 60 * 1000
    # task_timeout_ms 是单个任务的超时（毫秒）。
    task_timeout_ms: int = 15 * 60 * 1000


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class Bot:
    """飞书机器人。"""

    def __init__(
        self,
        cfg: FeishuConfig,
        engine: "Engine",
        sessions: "Manager",
    ) -> None:
        """创建机器人。engine 应由 bootstrap 装配好（含安全中间件）。

        这里补上 Go 版 NewBot 里的各项默认值：监听地址、路径、超时。
        """
        # 把默认值回填进 cfg（与 Go NewBot 行为一致）。
        if cfg.listen_addr == "":
            cfg.listen_addr = ":8080"
        if cfg.path == "":
            cfg.path = "/webhook"
        if cfg.approval_timeout_ms <= 0:
            cfg.approval_timeout_ms = 5 * 60 * 1000
        if cfg.task_timeout_ms <= 0:
            cfg.task_timeout_ms = 15 * 60 * 1000

        self.cfg = cfg
        self.engine = engine
        self.sessions = sessions

        # tenant token 缓存。
        self._token_lock = threading.Lock()
        self._token: str = ""
        self._token_exp: float = 0.0  # 距过期还有 60s 缓冲时的失效时刻（time.time() 基准）

        # 待审批请求：reqID -> Future[bool]。
        self._pending_lock = threading.Lock()
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._seq: int = 0

        # 由 start() 在事件循环里填充，供 HTTP 线程安全地回调。sync 落盘/解析用。
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: ThreadingHTTPServer | None = None

    # -----------------------------------------------------------------------
    # 启动：标准库 HTTP 服务 + 事件循环桥接
    # -----------------------------------------------------------------------

    async def start(self) -> None:
        """启动回调服务，直到被取消（asyncio.CancelledError）时优雅退出。

        飞书要求回调秒级返回，所以长任务必须异步处理。这里用 ThreadingHTTPServer
        在后台线程跑 HTTP，主协程持有事件循环；HTTP 线程里需要驱动 async 引擎时，
        用 `asyncio.run_coroutine_threadsafe` 把协程投到主循环执行——这就是
        "HTTP 线程"与"asyncio 事件循环"的桥接。
        """
        self._loop = asyncio.get_running_loop()
        host, port = _parse_addr(self.cfg.listen_addr)

        handler = functools.partial(_WebhookHandler, self)
        server = ThreadingHTTPServer((host, port), handler)
        self._server = server

        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            # 主协程空转，直到被外部 cancel（例如 bootstrap 关闭时）。
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            server.shutdown()
            server.server_close()
            raise

    # -----------------------------------------------------------------------
    # 群 → 会话：一个群 = 一个会话 = 一条独立上下文
    # -----------------------------------------------------------------------

    async def _handle_message(self, ev: dict[str, Any]) -> None:
        """处理一条收到的消息（在事件循环协程里跑，可 await 引擎）。"""
        # 机器人自己发的消息必须忽略，否则会自问自答形成无限循环。
        sender = ev.get("sender", {})
        if sender.get("sender_type") == "app":
            return

        text = extract_text(ev.get("message", {}).get("content", ""))
        if text.strip() == "":
            return

        chat_id = ev.get("message", {}).get("chat_id", "")
        if chat_id == "":
            return

        # 把"当前群"写进 ContextVar，供后续审批卡片读取该发往哪个群。
        token = _CHAT_ID.set(chat_id)
        try:
            # 一个群 = 一个会话 = 一条独立上下文。
            from tiny_claw.context.session import session_id_for_chat

            sess_id = session_id_for_chat("feishu", chat_id)
            sess: Session = self.sessions.get_or_create(
                sess_id, self.cfg.work_dir, "feishu:" + chat_id
            )

            # 任务级超时（Go 的 TaskTimeout）。
            res: Result = await asyncio.wait_for(
                self.engine.run(sess, text),
                timeout=self.cfg.task_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            await self.send_text(chat_id, "⏱️ 任务超时，已被终止。")
            return
        except Exception as exc:  # 引擎抛错也要回一条，让用户知道挂了
            await self.send_text(chat_id, "❌ 任务执行失败：" + str(exc))
            return
        finally:
            _CHAT_ID.reset(token)

        body = res.text
        if getattr(res, "exceeded", False):
            body += "\n\n（已达最大轮次，未正常收尾）"
        if body == "":
            body = "(未产出结论)"
        await self.send_text(chat_id, clip_runes(body, 8000))

    # -----------------------------------------------------------------------
    # 卡片交互：人类点"允许/拒绝"
    # -----------------------------------------------------------------------

    def handle_card_action(self, ev: dict[str, Any]) -> None:
        """处理卡片按钮回调（在 HTTP 线程里同步执行）。"""
        action = ev.get("action", {})
        val = action.get("value") or {}
        if not val:
            return
        act = val.get("act")
        req_id = val.get("req")
        if not req_id:
            return

        with self._pending_lock:
            fut = self._pending.get(req_id)
            if fut is not None:
                del self._pending[req_id]

        if fut is None:
            return
        # 用线程安全方式唤醒等在 Future 上的审批协程；非阻塞发送：
        # 审批请求可能已超时，此时没人接收，call_soon_threadsafe 不会阻塞。
        if self._loop is not None:
            self._loop.call_soon_threadsafe(fut.set_result, act == "approve")

    # -----------------------------------------------------------------------
    # 人工审批：把终端里的 y/N 换成一张可点按的卡片
    # -----------------------------------------------------------------------

    def approver(self) -> "Approver":
        """返回一个 stability.Approver，用于注册到引擎的安全中间件上。

        Approver 基类与 ApprovalRequest 在运行时才 import（避免包加载强依赖
        尚未产出的 stability 包）；这里定义一个内部子类，把"等卡片点击"的
        阻塞语义桥接到 asyncio Future 上。
        """
        from tiny_claw.stability import Approver, ApprovalRequest

        bot = self

        class _CardApprover(Approver):
            def __init__(self) -> None:
                pass

            async def approve(self, req: "ApprovalRequest") -> bool:
                return await bot._await_approval(req)

        return _CardApprover()

    def _next_req_id(self) -> str:
        with self._pending_lock:
            self._seq += 1
            return "apx%d" % self._seq

    async def _await_approval(self, req: "ApprovalRequest") -> bool:
        """发送审批卡片并等待人类点击，返回是否允许。"""
        chat_id = _CHAT_ID.get()
        if chat_id == "":
            # 拿不到目标群就无法请人类决策，此时默认拒绝是唯一安全选择。
            return False

        req_id = self._next_req_id()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        with self._pending_lock:
            self._pending[req_id] = fut

        try:
            await self.send_approval_card(chat_id, req, req_id)
        except Exception as exc:
            raise FeishuError("发送审批卡片失败: %s" % exc) from exc

        try:
            ok = await asyncio.wait_for(
                fut, timeout=self.cfg.approval_timeout_ms / 1000
            )
            return ok
        except asyncio.TimeoutError:
            return False

    # -----------------------------------------------------------------------
    # 飞书 Open API（urllib，零依赖）
    # -----------------------------------------------------------------------

    def _tenant_token(self) -> str:
        """获取 tenant_access_token，带本地缓存与 60s 提前失效缓冲。"""
        with self._token_lock:
            if self._token and time.time() < self._token_exp:
                return self._token

            payload = {
                "app_id": self.cfg.app_id,
                "app_secret": self.cfg.app_secret,
            }
            out = self._post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                payload,
                auth=False,
            )
            if out.get("code") != 0 or not out.get("tenant_access_token"):
                raise FeishuError(
                    "获取 tenant_access_token 失败: code=%s msg=%s"
                    % (out.get("code"), out.get("msg"))
                )
            self._token = out["tenant_access_token"]
            # 飞书返回的 expire 是秒；提前 60s 失效，避免用到过期 token。
            self._token_exp = time.time() + float(out.get("expire", 0)) - 60
            return self._token

    def _post(self, api_path: str, payload: Any, auth: bool) -> Any:
        """POST 到飞书开放 API（同步、阻塞），返回解析后的 dict。

        auth=True 时自动附带 Bearer tenant token。HTTP 客户端只用标准库
        urllib.request —— 本项目零第三方依赖。
        """
        data = json.dumps(payload).encode("utf-8")
        url = FEISHU_BASE + api_path
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if auth:
            req.add_header("Authorization", "Bearer " + self._tenant_token())

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read(4 << 20)
                status = resp.status
        except urllib.error.HTTPError as exc:  # 非 2xx 也带响应体，尽量报出原因
            raw = exc.read(4 << 20) if exc.fp else b""
            status = exc.code
            raise FeishuError(
                "feishu http %d: %s"
                % (status, clip_runes(raw.decode("utf-8", "replace"), 300))
            ) from exc

        if status < 200 or status >= 300:
            raise FeishuError(
                "feishu http %d: %s"
                % (status, clip_runes(raw.decode("utf-8", "replace"), 300))
            )
        return json.loads(raw.decode("utf-8"))

    async def send_text(self, chat_id: str, text: str) -> None:
        """发送纯文本消息到指定群（异步包装，阻塞 IO 丢到线程池）。"""
        await asyncio.to_thread(self._send_text_sync, chat_id, text)

    def _send_text_sync(self, chat_id: str, text: str) -> None:
        content = json.dumps({"text": text}, ensure_ascii=False)
        out = self._post(
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": content,
            },
            auth=True,
        )
        if out.get("code") != 0:
            raise FeishuError(
                "发送消息失败: code=%s msg=%s" % (out.get("code"), out.get("msg"))
            )

    async def send_approval_card(
        self, chat_id: str, req: "ApprovalRequest", req_id: str
    ) -> None:
        """发送一张带"允许/拒绝"按钮的审批卡片（异步包装）。"""
        await asyncio.to_thread(self._send_approval_card_sync, chat_id, req, req_id)

    def _send_approval_card_sync(
        self, chat_id: str, req: "ApprovalRequest", req_id: str
    ) -> None:
        # req 的字段名按全局 snake_case 约定取（tool_name / reason / input）。
        detail = "**工具**：%s\n\n**判定**：%s" % (req.tool_name, req.reason)
        cmd = command_preview(req.tool_name, req.input)
        if cmd != "":
            detail += "\n\n**内容**：\n```\n" + clip_runes(cmd, 800) + "\n```"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange",
                "title": {"tag": "plain_text", "content": "⚠️ 高危操作待审批"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": detail},
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "type": "primary",
                            "text": {"tag": "plain_text", "content": "允许执行"},
                            "value": {"act": "approve", "req": req_id},
                        },
                        {
                            "tag": "button",
                            "type": "danger",
                            "text": {"tag": "plain_text", "content": "拒绝"},
                            "value": {"act": "reject", "req": req_id},
                        },
                    ],
                },
            ],
        }
        content = json.dumps(card, ensure_ascii=False)
        out = self._post(
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            {
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": content,
            },
            auth=True,
        )
        if out.get("code") != 0:
            raise FeishuError(
                "发送审批卡片失败: code=%s msg=%s"
                % (out.get("code"), out.get("msg"))
            )


# ---------------------------------------------------------------------------
# HTTP 处理器
# ---------------------------------------------------------------------------

class _WebhookHandler(BaseHTTPRequestHandler):
    """飞书事件回调处理器（运行在 ThreadingHTTPServer 的后台线程里）。

    bot 通过 functools.partial 绑定到类属性，构造函数签名保持标准库要求。
    """

    bot: Bot

    def _bot(self) -> Bot:
        return type(self).bot

    def do_POST(self) -> None:  # noqa: N802  (标准库强制命名)
        if self.command != "POST":
            self.send_error(405, "method not allowed")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, 4 << 20))

        try:
            env = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "bad json")
            return

        bot = self._bot()

        # 飞书首次配置回调地址时会发 url_verification。
        if env.get("type") == "url_verification":
            if bot.cfg.verification_token and env.get("token") != bot.cfg.verification_token:
                self.send_error(403, "token mismatch")
                return
            self._send_json({"challenge": env.get("challenge")})
            return

        event_type = (env.get("header") or {}).get("event_type")
        if event_type == "im.message.receive_v1":
            self._send_json({"code": 0})  # 先回 200：飞书对回调超时很敏感
            # 长任务异步处理：把协程投到主事件循环执行，并在 HTTP 线程等待结果。
            coro = bot._handle_message(env.get("event", {}))
            loop = bot._loop
            if loop is None:
                return
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                fut.result(timeout=bot.cfg.task_timeout_ms / 1000)
            except Exception:
                # 任务失败已在 _handle_message 内部回消息，这里吞掉线程异常。
                pass

        elif event_type == "card.action.trigger":
            bot.handle_card_action(env.get("event", {}))
            self._send_json({"code": 0})

        else:
            self._send_json({"code": 0})

    def _send_json(self, obj: Any) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:  # 关掉标准库默认访问日志
        return


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _parse_addr(addr: str) -> tuple[str, int]:
    """把 ":8080" / "0.0.0.0:8080" 解析成 (host, port)。"""
    if addr.startswith(":"):
        return "", int(addr[1:])
    host, _, port = addr.rpartition(":")
    return host, int(port)


def extract_text(content: str) -> str:
    """从飞书消息 content（JSON 字符串）里取出纯文本，去掉首尾空白。"""
    try:
        m = json.loads(content)
        text = m.get("text", "")
    except (json.JSONDecodeError, AttributeError):
        text = content
    return text.strip()


def command_preview(tool_name: str, call_input: str) -> str:
    """给审批卡片生成"将要执行什么"的预览。

    工具名以 Go 源为准（Go 的 BashTool.Name() 返回 "bash"），故这里匹配 "bash"；
    Go 源码此处写死的是 "bash"，属跨层命名冲突（见交付说明）。
    """
    if tool_name != "bash":
        return clip_runes(call_input, 400)
    try:
        in_obj = json.loads(call_input)
        cmd = in_obj.get("command", "")
    except json.JSONDecodeError:
        cmd = call_input
    return clip_runes(cmd, 400)


def clip_runes(s: str, max_chars: int) -> str:
    """按字符截断，避免把中文/emoji 切成乱码。"""
    runes = list(s)
    if len(runes) <= max_chars:
        return s
    return "".join(runes[:max_chars]) + "…"
