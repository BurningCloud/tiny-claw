"""Provider 层共用的 HTTP 通道与若干零依赖小工具。

对应 Go 版的 http.go。这里刻意不引入任何第三方 HTTP 客户端：Harness 的
核心价值在控制流，多一个依赖就多一个"黑盒"，与专栏"大道至简"的取舍一致。
标准库 `urllib.request` 足以覆盖 JSON POST 的需求。

关于契约里提到的 "SSE 解析 + 重试"：本次移植的 Go 源文件（http.go /
claude.go / openai.go）**并没有**实现 SSE 流式解析，也**没有**在 doPost
里做重试——它们都是一次性的同步 JSON POST。因此这里忠实移植 Go 现有逻辑
（同步 POST + 上下文超长识别 + 截断 + 空对象兜底），没有凭空发明 SSE 与
重试循环。若后续 Go 主干补上了这两块，再回来补即可。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from tiny_claw.provider.base import ContextTooLongError

# 限制读取上限，避免异常响应把内存吃光（对应 Go 的 io.LimitReader 8<<20）。
MAX_BODY_BYTES = 8 << 20  # 8 MiB


class ProviderHTTPError(Exception):
    """非 2xx 或非 HTTP 层的网络错误。

    与 Go 版 `fmt.Errorf("provider: http %d ...")` 等价：把状态码、URL、
    响应体（截断后）打包成一个可读错误，供上层决定是否重试或降级。
    """

    def __init__(self, status_code: int, url: str, body: str) -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"provider: http {status_code} from {url}: {body}")


def do_post(
    url: str,
    headers: dict[str, str],
    body: object,
    *,
    timeout_ms: int = 600_000,
) -> bytes:
    """所有 Provider 共用的 JSON POST 通道（对应 Go 的 doPost）。

    - 把 body 编码成 JSON 写到请求体；
    - 设置 Content-Type 与调用方传入的 headers；
    - 用 urllib 发出请求（阻塞 IO，调用方应包在 asyncio.to_thread 里）；
    - 读取上限 MAX_BODY_BYTES，避免异常响应吃光内存；
    - 状态码非 2xx 时：若响应体像是"上下文超长"就抛 ContextTooLongError，
      否则抛 ProviderHTTPError。

    timeout_ms 默认 600000（10 分钟），对应 Go 版 http.Client{Timeout: 10 * time.Minute}。
    """
    data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)

    try:
        resp = urllib.request.urlopen(req, timeout=timeout_ms / 1000.0)
        status = resp.status
        raw = resp.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:  # 4xx / 5xx：先读完错误体再判断
        raw = exc.read(MAX_BODY_BYTES)
        if looks_like_context_too_long(raw):
            raise ContextTooLongError() from None
        raise ProviderHTTPError(
            status_code=exc.code,
            url=url,
            body=truncate(raw.decode("utf-8", "replace"), 512),
        ) from None
    except urllib.error.URLError as exc:  # 连接层错误（DNS、超时、拒绝等）
        raise ProviderHTTPError(
            status_code=0, url=url, body=str(getattr(exc, "reason", exc))
        ) from None

    if status < 200 or status >= 300:
        if looks_like_context_too_long(raw):
            raise ContextTooLongError() from None
        raise ProviderHTTPError(
            status_code=status,
            url=url,
            body=truncate(raw.decode("utf-8", "replace"), 512),
        ) from None
    return raw


def looks_like_context_too_long(body: bytes) -> bool:
    """跨厂商识别"上下文超长"。

    引擎拿到这个信号会触发紧急压缩，而不是把错误原样丢给用户。
    各厂商报错文案不一，这里用一组关键词做大小写不敏感的命中。
    """
    text = body.decode("utf-8", "replace").lower()
    for keyword in (
        "context_length_exceeded",
        "context length",
        "prompt is too long",
        "input is too long",
        "context window",
        "maximum context",
        "request too large",
        "string above max",
    ):
        if keyword in text:
            return True
    return False


def or_empty_object(value: str) -> str:
    """保证 JSON Schema / 工具入参永远是一段合法 JSON。

    空串或纯空白串统一补成 "{}"，避免下游 json.loads 直接崩。
    """
    if value.strip() == "":
        return "{}"
    return value


def truncate(text: str, n: int) -> str:
    """按字符数截断（Go 版按 rune，即 Unicode 码点，与 Python str 长度一致）。

    用于把超长的错误响应体截短后再塞进异常消息，避免一条报错刷屏。
    """
    if len(text) <= n:
        return text
    return text[:n] + "…"


__all__ = [
    "MAX_BODY_BYTES",
    "ProviderHTTPError",
    "do_post",
    "looks_like_context_too_long",
    "or_empty_object",
    "truncate",
]
