"""SDK transport adapter: commit the durable inbox before acknowledging an event.

The SDK default schedules callbacks and ACKs immediately. These synchronous entry
points run on its WS thread; the application database loop is a separate thread.
No SDK in-memory dedup or batching precedes PostgreSQL's unique inbox constraint.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from lark_oapi.channel import FeishuChannel
from lark_oapi.core.enum import LogLevel
from lark_oapi.core.json import JSON
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws import client as ws_client

from coreman.core.logging import get_logger

# 长连接断了飞书不会重投卡片回调，断连到重连之间的点击直接报「目标回调服务当前未在线」，
# 所以尽量缩短这段时间。websockets 默认 20 秒一次心跳、20 秒没回应才判断断开；
# 重连前随机等待和失败后的重试间隔由飞书下发（约 30 秒、120 秒）。每个机器人一个子进程、
# 一条连接，同时重连的只有这台机器上的几条，不需要那么长的错峰。
WS_PING_INTERVAL_SECONDS = 10
WS_PING_TIMEOUT_SECONDS = 10
RECONNECT_JITTER_SECONDS = 2
RECONNECT_INTERVAL_SECONDS = 10
_sdk_connect_kwargs = ws_client._ws_connect_kwargs


def _connect_kwargs() -> dict[str, Any]:
    return {
        **_sdk_connect_kwargs(),
        "ping_interval": WS_PING_INTERVAL_SECONDS,
        "ping_timeout": WS_PING_TIMEOUT_SECONDS,
    }


_sdk_configure = ws_client.Client._configure


def _configure(self: Any, conf: Any) -> None:
    """飞书每次握手都会下发重连参数；照收，只把等待时间压短。"""
    _sdk_configure(self, conf)
    _shorten_reconnect(self)


def _shorten_reconnect(client: Any) -> None:
    client._reconnect_nonce = min(client._reconnect_nonce, RECONNECT_JITTER_SECONDS)
    client._reconnect_interval = min(client._reconnect_interval, RECONNECT_INTERVAL_SECONDS)


ws_client._ws_connect_kwargs = _connect_kwargs
ws_client.Client._configure = _configure

# 回复按钮点击的应答：已收下（worker 还可能拒绝，所以不说「已发送」）、这一行点过了、
# 只有提问人能点。选择题等其他卡片点击照旧只回一个对勾，结果由后续卡片更新。
TOASTS: dict[str, dict[str, str]] = {
    "queued": {"type": "success", "content": "已收到"},
    "duplicate": {"type": "info", "content": "已选过"},
    "not_requester": {"type": "warning", "content": "只有提问人可以选择"},
}
DEFAULT_TOAST = {"type": "info", "content": "✓"}


def card_toast(accepted: object) -> dict[str, str]:
    """回调应答里的 toast：按 accept 报告的点击结果选文案。"""
    toast = TOASTS.get(accepted) if isinstance(accepted, str) else None
    return dict(toast or DEFAULT_TOAST)


class DurableChannel(FeishuChannel):  # type: ignore[misc]  # SDK has no py.typed marker
    def __init__(
        self,
        *,
        accept: Callable[[dict[str, Any]], Awaitable[object]],
        app_id: str,
        app_secret: str,
        encrypt_key: str = "",
        verification_token: str = "",
        ack_timeout: float = 2.0,
    ) -> None:
        self._application_loop = asyncio.get_running_loop()
        self._application_thread = threading.get_ident()
        self._accept = accept
        self._ack_timeout = ack_timeout
        super().__init__(
            app_id=app_id,
            app_secret=app_secret,
            encrypt_key=encrypt_key,
            verification_token=verification_token,
            log_level=LogLevel.ERROR,
        )
        self._log = get_logger(__name__).bind(app_id=app_id)
        self._lost_at: float | None = None
        self.on("reconnecting", self._on_reconnecting)
        self.on("reconnected", self._on_reconnected)

    def _on_reconnecting(self) -> None:
        """SDK 判定连接断开、开始重连时调用（在它的 WS 线程上），随后才读重连参数。"""
        self._lost_at = time.monotonic()
        client = getattr(self, "_ws_client", None)
        if client is not None:  # 首次连接就失败时还没收到过飞书下发的参数
            _shorten_reconnect(client)
        self._log.warning("feishu_ws_reconnecting")

    def _on_reconnected(self) -> None:
        lost_at, self._lost_at = self._lost_at, None
        offline = round(time.monotonic() - lost_at, 1) if lost_at is not None else None
        self._log.warning("feishu_ws_reconnected", offline_s=offline)

    def _build_dispatcher(self) -> Any:
        return (
            EventDispatcherHandler.builder(
                self._config.encrypt_key or "",
                self._config.verification_token or "",
                LogLevel.ERROR,
            )
            .register_p2_im_message_receive_v1(self._on_p2_im_message_receive_v1)
            .register_p2_card_action_trigger(self._on_p2_card_action_trigger)
            .register_p2_im_chat_member_bot_added_v1(self._on_p2_bot_added)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._persist_before_ack)
            .build()
        )

    def _persist_before_ack(self, data: Any) -> object:
        """落库提交之后才应答；返回 accept 的结果（回复按钮点击的处理结果，其余为 None）。"""
        if threading.get_ident() == self._application_thread:
            raise RuntimeError("durable event callback requires transport thread")
        try:
            raw = json.loads(JSON.marshal(data) or "{}")
            if not isinstance(raw, dict):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise RuntimeError("invalid platform event") from exc

        async def persist() -> object:
            return await self._accept(raw)

        future = asyncio.run_coroutine_threadsafe(persist(), self._application_loop)
        try:
            return future.result(timeout=self._ack_timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise RuntimeError("durable inbox timed out; redelivery required") from None
        except Exception:
            # SDK logs this exception. Never propagate payload, SQL parameters or credentials.
            raise RuntimeError("durable inbox unavailable; redelivery required") from None

    def _on_p2_im_message_receive_v1(self, data: Any) -> None:
        self._persist_before_ack(data)

    def _on_p2_card_action_trigger(self, data: Any) -> P2CardActionTriggerResponse:
        # 3 秒应答窗口：这里只落库，改卡片（置灰已点的按钮）交给出站循环。
        accepted = self._persist_before_ack(data)
        return P2CardActionTriggerResponse({"toast": card_toast(accepted)})

    def _on_p2_bot_added(self, data: Any) -> None:
        self._persist_before_ack(data)
