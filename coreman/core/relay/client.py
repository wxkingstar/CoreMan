"""运行时 HTTP 客户端。

GET /health（5 秒）与 GET /v1/models，以及
POST /v1/chat/completions 的流式对话。

/health 每次调用都会让 relay fork 一个 CLI，不要高频探测。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from coreman.core.db.models import RelayServer
from coreman.core.relay.sse import SseEvent, SseParser
from coreman.core.runtime_nodes.transport import (
    TOTAL_TIMEOUT_EXTENSION,
    ReverseTransport,
    RuntimeQueueTimeout,
)

_AUTH_MARKERS = ("not logged in", "/login", "401")

# verbosity 档位到 relay settings.outputStyle 的映射；1 档不传 settings。
VERBOSITY_OUTPUT_STYLES = {2: "verbosity-normal", 3: "verbosity-quiet", 4: "verbosity-silent"}


class RelayError(Exception):
    """访问 relay 接口失败。"""


class IncompleteResultError(RelayError):
    """流正常读完，却既没有确认终态（finish_reason），也没有 relay 回传的错误。"""


class RelayBusyError(RelayError):
    """运行时节点满载：对话排队超过上限仍未开始执行（没有任何副作用）。"""


@dataclass
class RelayHealth:
    """一次 /health 探测的结果。"""

    status: str  # healthy / down / auth_fail / timeout
    detail: str | None
    latency_ms: int
    backend: str | None = None
    version: str | None = None
    mode: str | None = None


@dataclass
class ChatRequest:
    """一次 relay 对话请求的全部输入。"""

    model: str
    system_prompt: str
    # 纯文本，或多模态 content parts（text / image_url / file_url）——后者由
    # `ContentBuilder` 组装，`to_body` 原样放进 messages[1].content。
    user_content: str | list[dict[str, Any]]
    working_dir: str
    session_id: str
    backend: str
    effort: str | None = None
    verbosity_level: int = 1
    env_vars: dict[str, str] = field(default_factory=dict)
    max_turns: int = 80

    def to_body(self) -> dict[str, Any]:
        """拼出 POST /v1/chat/completions 的请求体。

        `max_turns` 与 `settings` 仅 claude backend 携带；`effort`、`env_vars` 非空才带；
        `session_id` 原样透传（cron 与体检传空字符串）。
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": self.user_content},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "working_dir": self.working_dir,
            "session_id": self.session_id,
        }
        if self.backend == "claude":
            body["max_turns"] = self.max_turns
            style = VERBOSITY_OUTPUT_STYLES.get(self.verbosity_level)
            if style:
                body["settings"] = json.dumps({"outputStyle": style})
        if self.effort:
            body["effort"] = self.effort
        if self.env_vars:
            body["env_vars"] = dict(self.env_vars)
        return body


RUNTIME_BASE_URL = "http://runtime"


class RelayClient:
    """单个运行时实例的客户端。"""

    def __init__(
        self,
        base_url: str = RUNTIME_BASE_URL,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """初始化客户端。

        Args:
            base_url: 请求基址；反向通道只取路径，主机名无意义
            http: 自定义 httpx.AsyncClient（外部注入的不由本类关闭）
            timeout: 默认客户端各阶段超时秒数
            transport: 自建客户端使用的传输层；运行时实例请用 `RelayClient.for_relay`

        Raises:
            ValueError: http 与 transport 都没给——不再按地址猜测传输方式
        """
        if http is None and transport is None:
            raise ValueError("RelayClient 需要 http 或 transport；运行时实例请用 for_relay")
        self._http = http or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(timeout), transport=transport, trust_env=False
        )
        self._owns = http is None

    @classmethod
    def for_relay(cls, relay: RelayServer, *, timeout: float = 5.0) -> RelayClient:
        """按实例所属运行时节点建反向通道客户端。

        Raises:
            RelayError: 实例未绑定运行时节点（独立中继实例已不再支持）
        """
        if relay.runtime_node_id is None:
            raise RelayError("运行时实例未绑定节点")
        transport = ReverseTransport(relay.runtime_node_id, relay.model_provider)
        return cls(RUNTIME_BASE_URL, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        """关闭自建的连接；外部注入的 http 不关。"""
        if self._owns:
            await self._http.aclose()

    async def health(self) -> RelayHealth:
        """探测 /health，永不抛异常。

        Returns:
            RelayHealth：status 为 healthy / down / auth_fail / timeout
        """
        started = time.monotonic()
        try:
            r = await self._http.get("/health")
        except httpx.TimeoutException:
            return RelayHealth("timeout", "健康检查超时", _ms(started))
        except httpx.HTTPError as exc:
            return RelayHealth("down", f"连接失败: {type(exc).__name__}", _ms(started))
        latency = _ms(started)
        text = r.text[:200]
        if r.status_code >= 300:
            return RelayHealth("down", f"HTTP {r.status_code}: {text}", latency)
        try:
            body: Any = r.json()
        except ValueError:
            return RelayHealth("down", f"非 JSON 响应: {text}", latency)
        if not isinstance(body, dict):
            return RelayHealth("down", f"非 JSON 对象: {text}", latency)
        if body.get("status") == "healthy":
            return RelayHealth(
                "healthy",
                None,
                latency,
                backend=_str(body.get("backend")),
                version=_str(body.get("version")),
                mode=_str(body.get("mode")),
            )
        lowered = text.lower()
        status = "auth_fail" if any(m in lowered for m in _AUTH_MARKERS) else "down"
        return RelayHealth(status, text, latency)

    async def models(self) -> list[str]:
        """取 /v1/models 的 data[].id 列表。

        Returns:
            模型 id 列表；data 不是数组时视为空

        Raises:
            RelayError: HTTP 非 2xx、连接失败或响应无法解析时抛出
        """
        try:
            r = await self._http.get("/v1/models")
            if r.status_code >= 300:
                raise RelayError(f"HTTP {r.status_code}")
            data: Any = r.json().get("data", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise RelayError(f"获取模型列表失败: {type(exc).__name__}") from exc
        if not isinstance(data, list):
            return []
        return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]

    async def chat_stream(
        self,
        request: ChatRequest,
        *,
        total_timeout: float,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
    ) -> AsyncGenerator[SseEvent, None]:
        """流式对话：逐行解析 SSE 并产出事件，见到 `[DONE]` 或流断即结束。

        调用方 `aclose()` 会退出 `self._http.stream(...)` 上下文，也就是断开连接——relay 把
        断连当作「停止任务」，所以想让任务继续跑就不要提前关生成器。

        Args:
            request: 请求内容
            total_timeout: 整条流的总时限（秒），在「等下一行」的间隙判定
            connect_timeout: 连接（含连接池）超时秒数
            read_timeout: 首字节与字节间空闲超时秒数

        Yields:
            SseEvent：正文/思考/工具/用量/错误/收尾事件，最后补上 flush 出的问卷事件

        Raises:
            RelayError: HTTP 非 200、连接失败、读超时或总时长超限
            IncompleteResultError: 流结束时没有 finish_reason，也没有 relay 回传的错误
        """
        parser = SseParser(request.backend)
        timeout = httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=30.0, pool=connect_timeout
        )
        deadline = time.monotonic() + total_timeout
        try:
            # 声明总时限：经反向通道时节点满载可以排队，调用期限也按它算（直连 relay 忽略）。
            async with self._http.stream(
                "POST",
                "/v1/chat/completions",
                json=request.to_body(),
                timeout=timeout,
                extensions={TOTAL_TIMEOUT_EXTENSION: total_timeout},
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread())[:500].decode(errors="replace")
                    raise RelayError(f"HTTP {resp.status_code}: {body}")
                lines = resp.aiter_lines()
                while not parser.done:
                    line = await _next_line(lines, deadline)
                    if line is None:
                        break
                    for event in parser.feed_line(line):
                        yield event
        except TimeoutError as exc:
            raise RelayError(f"SSE timeout: 总时长超过 {total_timeout:.0f} 秒") from exc
        except httpx.ReadTimeout as exc:
            raise RelayError(f"SSE timeout: {read_timeout:.0f} 秒无字节") from exc
        except RuntimeQueueTimeout as exc:
            raise RelayBusyError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise RelayError(f"连接失败: {type(exc).__name__}") from exc
        for event in parser.flush():
            yield event
        # relay 已经以 x_relay_error 交代了失败原因（有的驱动失败时不补 finish chunk）：原因已经
        # 交给调用方按错误分类，再抛「未确认终态」只会让调用方丢掉这段原话。
        if not parser.saw_finish and not parser.saw_error:
            raise IncompleteResultError("incomplete_result: SSE 未返回确认终态")


async def _next_line(lines: AsyncIterator[str], deadline: float) -> str | None:
    """取下一行；流结束返回 None，超出总时限抛 TimeoutError。

    总时限只圈住「等下一行」这段 await，不跨 yield——跨 yield 的话超时会把取消抛进调用方
    两次取值之间正在做的事情里（写库、发消息），而不是变成这里的 RelayError。
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    async with asyncio.timeout(remaining):
        try:
            return await anext(lines)
        except StopAsyncIteration:
            return None


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _str(value: Any) -> str | None:
    return str(value) if value is not None else None
