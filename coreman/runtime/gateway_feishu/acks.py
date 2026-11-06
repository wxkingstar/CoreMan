"""调度员在单人群里收到消息就打「处理中」表情，调度回合结束时撤掉。

单人群里那位真人说的每句话调度员都会回应（见 core/chat/dispatch），但要先等群安静一会儿、
再跑一轮调度，表情要几秒后才出现。这里在网关收到消息时就打上，让人马上知道有人接了。

表情只记在网关进程内存里：调度回合在同一个网关进程里发终稿时，撤掉这一批（锚点消息及之前
收到的）的表情。网关重启或这一批没有排出调度回合（例如正忙、被静音）时，超过
`ACK_TTL_SECONDS` 由每轮出站顺手撤掉，不会一直挂在消息上。表情尽力而为，失败不影响回答。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from coreman.core.chat import dispatch
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuError

if TYPE_CHECKING:
    from coreman.runtime.gateway_feishu.transport import FeishuTransport

log = get_logger(__name__)

ACK_TTL_SECONDS = 180
EMOJI = "Typing"


@dataclass
class _Ack:
    message_id: str
    at: float
    reaction_id: str | None = None
    cleared: bool = False


class Acks:
    def __init__(self, transport: FeishuTransport) -> None:
        self._transport = transport
        self._by_chat: dict[str, list[_Ack]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def has(self, chat_id: str | None, message_id: str | None) -> bool:
        return any(a.message_id == message_id for a in self._by_chat.get(chat_id or "", []))

    def start(self, chat_id: str, message_id: str) -> None:
        """收到消息时调用：后台查是不是单人群、是就打表情，不拖住入站确认。"""
        if self.has(chat_id, message_id):
            return  # 平台重推同一条
        task = asyncio.create_task(self._ack(chat_id, message_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _ack(self, chat_id: str, message_id: str) -> None:
        from coreman.runtime.gateway_feishu.transport import api_id

        transport = self._transport
        try:
            humans = await dispatch.human_count_with(
                transport.client, str(transport.bot_id), chat_id
            )
            if humans != 1 or self.has(chat_id, message_id):
                return
            entry = _Ack(message_id, time.monotonic())
            self._by_chat.setdefault(chat_id, []).append(entry)
            result = await transport.call(
                "POST",
                f"/open-apis/im/v1/messages/{api_id(message_id)}/reactions",
                json={"reaction_type": {"emoji_type": EMOJI}},
            )
            entry.reaction_id = (result.get("data") or {}).get("reaction_id") or None
            if entry.cleared:
                # 打表情期间这一批已经处理完了：补撤。
                await self._delete(entry)
        except (FeishuError, ValueError, TypeError) as exc:
            log.info("dispatch_ack_failed", error=type(exc).__name__)

    async def clear(self, chat_id: str | None, upto_message_id: str | None) -> None:
        """撤掉这个群里锚点消息及之前收到的表情（同一批合并处理的几句）。"""
        acks = self._by_chat.get(chat_id or "", [])
        index = next((i for i, a in enumerate(acks) if a.message_id == upto_message_id), None)
        if index is None:
            return
        done, self._by_chat[chat_id or ""] = acks[: index + 1], acks[index + 1 :]
        for entry in done:
            entry.cleared = True
            await self._delete(entry)

    async def expire(self) -> None:
        cutoff = time.monotonic() - ACK_TTL_SECONDS
        for chat_id, acks in list(self._by_chat.items()):
            stale = [a for a in acks if a.at < cutoff]
            if not stale:
                continue
            self._by_chat[chat_id] = [a for a in acks if a.at >= cutoff]
            for entry in stale:
                entry.cleared = True
                await self._delete(entry)

    async def _delete(self, entry: _Ack) -> None:
        from coreman.runtime.gateway_feishu.transport import api_id

        if entry.reaction_id is None:
            return
        reaction_id, entry.reaction_id = entry.reaction_id, None
        try:
            await self._transport.call(
                "DELETE",
                f"/open-apis/im/v1/messages/{api_id(entry.message_id)}/reactions/"
                f"{api_id(reaction_id)}",
            )
        except (FeishuError, ValueError, TypeError) as exc:
            log.info("dispatch_ack_clear_failed", error=type(exc).__name__)
