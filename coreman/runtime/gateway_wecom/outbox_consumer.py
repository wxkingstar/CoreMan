"""出站箱消费：把 `outbox` 的条目发成企微主动消息 / 欢迎语（spec §6.4、§7.3）。

四种 kind 的处置：

| kind | 处置 |
| --- | --- |
| `send` | `aibot_send_msg`（`payload.card` 发 `template_card`，否则 markdown），按会话频控 |
| `welcome` | `aibot_respond_welcome_msg`，必须 5 秒内发出，所以不参与频控 |
| `card_update` | `aibot_respond_update_msg`，5 秒窗口所以不参与频控；缺 `req_id` → `skipped` |
| `stream_finish` | `skipped`（流的收尾由 pusher 管，出站箱不碰） |

频控只在内存里记：上一条什么时候发的、企微有没有回过 846607。进程重启后从零开始——真撞上
频控大不了再被拒一次，比把状态写进库划算。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession
from websockets.exceptions import WebSocketException

from coreman.core.bus import outbox
from coreman.core.chat.reachability import private_target_valid
from coreman.core.db.models import OutboxItem
from coreman.core.logging import get_logger
from coreman.runtime.gateway_wecom.ws_client import (
    DeliveryNotSent,
    DeliveryRejected,
    DeliveryUncertain,
)

if TYPE_CHECKING:
    from coreman.runtime.gateway_wecom.runner import BotRunner

UNSUPPORTED_KINDS = ("stream_finish",)
# 频控一族（846607 频控、45009 接口调用超限、-1 系统繁忙）：该会话先静默一阵再发。
RATE_LIMIT_CODES = frozenset({846607, 45009, -1})


class OutboxConsumer:
    """一个 bot 的出站箱消费者。

    Attributes:
        paused: 排空第一步就置上，之后不再领新条目（留给下一任实例）
        min_chat_interval: 同一会话两条主动消息的最小间隔
        rate_limit_backoff: 收到 846607 之后该会话静默多久
    """

    def __init__(
        self,
        runner: BotRunner,
        *,
        min_chat_interval: float = 1.0,
        rate_limit_backoff: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runner = runner
        self.min_chat_interval = min_chat_interval
        self.rate_limit_backoff = rate_limit_backoff
        self.clock = clock
        self.paused = False
        self._last_sent: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}
        self._log = get_logger(__name__).bind(bot_key=runner.bot.bot_key)

    def note_rate_limited(self, chat_id: str) -> None:
        """企微回了 846607：这个会话先停一段时间。"""
        self._blocked_until[chat_id] = self.clock() + self.rate_limit_backoff
        self._log.warning("wecom_chat_rate_limited", backoff_s=self.rate_limit_backoff)

    def wait_seconds(self, chat_id: str) -> float:
        """还要等多少秒才能给这个会话发下一条（0 = 现在就能发）。"""
        now = self.clock()
        waits = [self._blocked_until.get(chat_id, 0.0) - now]
        last = self._last_sent.get(chat_id)
        if last is not None:
            waits.append(last + self.min_chat_interval - now)
        return max([0.0, *waits])

    def online(self) -> bool:
        """连接已经订阅成功，帧写得出去。"""
        return self.runner.ws.state == "subscribed"

    async def consume(self) -> None:
        """领一条发一条，直到队列空（或连接不可用）。锁防重入。

        连接还没订阅上（刚认领还在建连、断线重连的退避窗口、订阅被拒）就一条都不领：领了也
        发不出去，只会平白耗掉重试额度，还把同组后续项一起拖进退避。
        """
        if self.paused:
            return
        async with self.runner.lock:
            while not self.paused and self.online():
                async with self.runner.factory() as session:
                    item = await outbox.claim_next(session, bot_id=self.runner.bot.id)
                    keep_going = item is None or await self._handle(session, item)
                    await session.commit()
                if item is None or not keep_going:
                    return

    async def _handle(self, session: AsyncSession, item: OutboxItem) -> bool:
        """处理一条；返回 False 表示连接不可用，这一轮别再领了。"""
        if item.lease_generation is not None and item.lease_generation != self.runner.generation:
            # 上一任租约排的队：它的会话上下文（req_id、卡片实体）对这条连接已经没意义了。
            await outbox.mark_skipped(session, item.id, "stale generation")
            return True
        if item.kind in UNSUPPORTED_KINDS:
            await outbox.mark_skipped(session, item.id, "出站箱不支持")
            return True
        chat_id = str((item.target or {}).get("chat_id") or "")
        # 内部键（`_attempt_id` 等）只给总线自己用，拼帧只读剥掉它们之后的内容。
        payload = outbox.public_payload(item.payload)
        if item.kind == "send":
            if not await private_target_valid(session, item):
                await outbox.mark_skipped(session, item.id, "接收用户停用或私聊绑定已变更")
                return True
            wait = self.wait_seconds(chat_id)
            if wait > 0:
                # 频控不是失败：放回队列推迟重发，不涨 attempts、不消耗重试额度。
                await outbox.defer(session, item.id, wait)
                self._log.info("outbox_deferred", outbox_id=item.id, wait_s=round(wait, 2))
                return True
            req_id = str(uuid.uuid4())
            card = payload.get("card")
            body = (
                {"chatid": chat_id, "msgtype": "template_card", "template_card": card}
                if isinstance(card, dict)
                else {
                    "chatid": chat_id,
                    "msgtype": "markdown",
                    "markdown": {"content": str(payload.get("markdown") or "")},
                }
            )
            frame: dict[str, Any] = {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": req_id},
                "body": body,
            }
        elif item.kind == "card_update":
            # 更新卡片是对那次点击的被动回复：必须原样带回 req_id，而且只有 5 秒窗口，
            # 所以既不按会话频控排队（排一秒就废了），也不记进 `_last_sent`。
            req_id = str((item.target or {}).get("req_id") or "")
            card = payload.get("card")
            if not req_id or not isinstance(card, dict):
                await outbox.mark_skipped(session, item.id, "缺少 req_id 或 card")
                return True
            frame = {
                "cmd": "aibot_respond_update_msg",
                "headers": {"req_id": req_id},
                "body": {"response_type": "update_template_card", "template_card": card},
            }
        elif item.kind == "welcome":
            req_id = str((item.target or {}).get("req_id") or "")
            if not req_id:
                await outbox.mark_skipped(session, item.id, "缺少 req_id")
                return True
            frame = {
                "cmd": "aibot_respond_welcome_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": "text",
                    "text": {"content": str(payload.get("text") or "")},
                },
            }
        else:
            await outbox.mark_skipped(session, item.id, "未知 kind")
            return True
        if not self.online():
            # 领到之后、写帧之前连接没了：原样放回队列，不算一次尝试。
            await outbox.defer(session, item.id, 0.0)
            return False
        # 先把这次尝试落库，再登记回执、写帧；进程死在等回执的路上由 scheduler 回收。
        await outbox.begin_attempt(session, item, req_id)
        try:
            await self.runner.ws.send_confirmed(frame)
        except DeliveryNotSent as exc:
            # 字节根本没写出去：结果确定是「没送到」，按普通失败退避重试。
            await outbox.mark_failed(session, item.id, f"not_sent: {exc}")
            self._log.info("outbox_not_sent", outbox_id=item.id, reason=str(exc))
            return False
        except DeliveryRejected as exc:
            if exc.errcode in RATE_LIMIT_CODES and chat_id:
                self.note_rate_limited(chat_id)
            # 平台明确拒了：退避重试，额度用完才判死（同组后续项随即释放）。
            await outbox.mark_failed(session, item.id, str(exc))
            self._log.warning("outbox_rejected", outbox_id=item.id, errcode=exc.errcode)
            return True
        except (DeliveryUncertain, RuntimeError, WebSocketException, OSError) as exc:
            # 写出去了却没等到回执：结果未知，有界重试（下次换新的 `_attempt_id`）。
            await outbox.unknown_attempt(session, item.id, f"delivery_unknown: {exc}")
            self._log.warning("outbox_delivery_unknown", outbox_id=item.id)
            return False
        if chat_id:
            self._last_sent[chat_id] = self.clock()
        await outbox.mark_sent(session, item.id)
        self._log.info("outbox_sent", outbox_id=item.id, kind=item.kind)
        return True
