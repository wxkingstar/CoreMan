"""流式卡片上「停止」按钮的点击：核对后停掉这一条回复。

只认这一轮的提问人（群里别人点了不算），并且点的必须是这条任务自己的流式卡片（按投递记录里
的消息 ID 核对，转发出去或伪造的回调停不了别人的任务）。停下后 worker 照常收尾：已经写出的
内容留在卡片里，末尾加「任务已被用户停止」。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.chat import stream_stop
from coreman.core.db.models import Bot, FeishuDelivery, InboundEvent, Task
from coreman.runtime.worker.context import TaskContext


async def handle(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    target_id = stream_stop.parse_card_task_id(str(action.get("task_id") or ""))
    target = await session.get(Task, target_id) if target_id else None
    clicker = str(ctx.task.payload.get("platform_user_id") or "")
    delivery = await session.get(FeishuDelivery, target_id) if target_id else None
    if (
        target is None
        or target.bot_id != bot.id
        or target.kind != "chat"
        or delivery is None
        or not delivery.message_id
        or delivery.message_id != str(inbound.reply_context.get("message_id") or "")
    ):
        ctx.log.info("stream_stop_ignored", target_task_id=target_id)
        return "ignored"
    if not clicker or str(target.payload.get("platform_user_id") or "") != clicker:
        return "not_requester"
    if not await tasks.request_cancel(session, target.id, "user_stop"):
        return "late"
    return "stopped"
