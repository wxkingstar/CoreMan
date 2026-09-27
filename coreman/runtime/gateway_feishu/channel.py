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
from collections.abc import Awaitable, Callable
from typing import Any

from lark_oapi.channel import FeishuChannel
from lark_oapi.core.enum import LogLevel
from lark_oapi.core.json import JSON
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

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
