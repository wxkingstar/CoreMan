"""网关这一侧的回复按钮：应答回调前的判定，以及这一轮开始后把按钮行置灰。

应答回调（3 秒窗口）只做两件判定再落库：点击人不是提问人就拒绝；会话类型按真实私聊记录
改判（点击人与本机器人的私聊记录正是这个会话才算私聊，否则按群）。置灰由 worker 在这一轮
真正开始时排出站，这里的出站循环执行：

- 流式主卡是卡片实体：按 message_id 找到投递记录拿 card_id，用 batch_update 局部更新，序号走
  `transport.sequence()`，与这张卡之前的所有操作共用一个严格递增的序号空间。按钮只出现在终稿
  里，终稿之前已经关了流式，不会撞上「流式期间回调不能更新卡片」。投递记录跟着流在完成 1 小时
  后清理，之后的点击照常发出消息，只是不再置灰；
- 续卡是普通卡片消息：出站记录里存着卡片 JSON，改好后整卡 PATCH，并把改过的卡存回去，
  同一张卡的另一行再被点时不会把前一行改回来。
"""

from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat.card_replies import REPLY_BUTTON, clicked
from coreman.core.chat.reachability import private_chat_of
from coreman.core.db.models import FeishuDelivery, OutboxItem, TaskStream
from coreman.core.feishu_cards.reply_buttons import UsedRow, batch_actions, mark_card
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuError
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import InboundBot, enqueue_inbound

if TYPE_CHECKING:
    from coreman.runtime.gateway_feishu.transport import FeishuTransport

log = get_logger(__name__)
# 点击的结果，决定回调应答里的 toast。
Outcome = Literal["queued", "duplicate", "not_requester"]


async def settle(session: AsyncSession, bot_id: uuid.UUID, message: InboundMessage) -> bool:
    """应答前的判定：不是回复按钮点击或允许入队返回 True；提问人以外的人点击返回 False。

    会话类型在这里定下来：按钮 value 里没有它，也不信任回调里的任何说法。
    """
    meta = clicked(message.reply_context)
    if meta is None or message.kind != "message":
        return True
    if meta.get("requester") != message.sender.platform_user_id:
        return False
    single = await private_chat_of(
        session, bot_id, "feishu", message.sender.platform_user_id, message.chat_id
    )
    chat_type: Literal["single", "group"] = "single" if single else "group"
    message.chat_type = chat_type
    message.reply_context = {**message.reply_context, "chat_type": chat_type}
    return True


def is_reply_click(message: InboundMessage) -> bool:
    return message.kind == "message" and REPLY_BUTTON in message.reply_context


async def admit(
    session: AsyncSession, bot: InboundBot, message: InboundMessage, *, lease_generation: int
) -> Outcome | None:
    """应答窗口里的入站落库：回复按钮先判定提问人与会话类型。返回回复按钮点击的结果，
    其余事件返回 None。调用方负责提交。"""
    if not await settle(session, bot.id, message):
        return "not_requester"
    task = await enqueue_inbound(session, bot, message, lease_generation=lease_generation)
    if not is_reply_click(message):
        return None
    return "queued" if task is not None else "duplicate"


async def mark_used(transport: FeishuTransport, item: OutboxItem) -> None:
    """出站循环里执行置灰。卡片不认改动（字段、ID 冲突）只记一笔，这是锦上添花，不重试。"""
    from coreman.runtime.gateway_feishu.transport import CARD_CONTENT_ERRORS, api_id, card_json

    used = UsedRow.parse(item.payload.get("_reply_used"))
    user = str(item.payload.get("_operator") or "")
    mid = api_id(item.target.get("message_id"))
    if used is None:
        return
    async with transport.factory() as session:
        delivery = await session.scalar(
            select(FeishuDelivery)
            .join(TaskStream, TaskStream.task_id == FeishuDelivery.task_id)
            .where(TaskStream.bot_id == transport.bot_id, FeishuDelivery.message_id == mid)
        )
    try:
        if delivery is not None:
            if delivery.card_id and delivery.is_static:
                card_id = api_id(delivery.card_id)
                await transport.call(
                    "POST",
                    f"/open-apis/cardkit/v1/cards/{card_id}/batch_update",
                    json={
                        "sequence": await transport.sequence(delivery.task_id),
                        "actions": card_json(batch_actions(used, user)),
                    },
                )
            return
        async with transport.factory() as session:
            sent = await session.scalar(
                select(OutboxItem)
                .where(
                    OutboxItem.bot_id == transport.bot_id,
                    OutboxItem.status == "sent",
                    OutboxItem.payload["_feishu_message_id"].astext == mid,
                )
                .order_by(OutboxItem.id.desc())
                .limit(1)
            )
            card = sent.payload.get("card") if sent is not None else None
            if sent is None or not isinstance(card, dict) or card.get("schema") != "2.0":
                return
            updated = copy.deepcopy(card)
            if not mark_card(updated, used, user):
                return
            await transport.call(
                "PATCH",
                f"/open-apis/im/v1/messages/{mid}",
                json={"content": card_json({k: v for k, v in updated.items() if k != "task_id"})},
            )
            sent.payload = {**sent.payload, "card": updated}
            await session.commit()
    except FeishuError as exc:
        if exc.code not in CARD_CONTENT_ERRORS:
            raise
        log.warning("feishu_reply_used_rejected", code=exc.code, item_id=item.id)
