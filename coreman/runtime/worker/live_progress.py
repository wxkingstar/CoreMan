"""运行中卡片「看看进度」的点击：引用那张卡回一条实时进度，不改卡片、不调用模型。

流式卡片不能用回调应答改（飞书要求先关流式），所以点击时网关只回一个 toast，
进度由这里排一条引用回复送到。群里谁点都行：卡片上的进度本来就是大家看得到的。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.chat import busy_choice, liveness
from coreman.core.db.models import Bot, FeishuDelivery, InboundEvent, Task, TaskStream
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.context import TaskContext


async def handle(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    """返回结果名（写进任务 result 方便排查）。"""
    target_id = liveness.parse_live_task_id(str(action.get("task_id") or ""))
    card_message = str((inbound.reply_context or {}).get("message_id") or "")
    delivery = await session.get(FeishuDelivery, target_id) if target_id else None
    task = await session.get(Task, target_id) if target_id else None
    # 只认我们自己发出的那张卡：任务 id 写在按钮里，点的消息必须就是这个任务的流式卡片。
    if (
        delivery is None
        or task is None
        or task.bot_id != bot.id
        or not card_message
        or delivery.message_id != card_message
    ):
        ctx.log.info("live_progress_ignored", target_task_id=target_id)
        return "ignored"
    stream = await session.get(TaskStream, target_id, populate_existing=True)
    now = datetime.now(UTC)
    if stream is None or stream.is_complete:
        text = msg("live_progress_done", ctx.locale)
        result = "done"
    else:
        status = liveness.status_heading(
            running_since=stream.running_since,
            activity=stream.activity,
            complete=False,
            now=now,
        )
        detail = busy_choice.progress_text(
            status=task.status,
            seconds=(now - stream.running_since).total_seconds(),
            thinking_md=stream.thinking_md,
            reply_text=stream.pending_text,
            quiet_seconds=(now - stream.updated_at).total_seconds(),
            locale=ctx.locale,
            subject="live",
        )
        text = f"{status}\n{detail}"
        result = "progress"
    await outbox.add(
        session,
        bot_id=bot.id,
        platform=bot.platform,
        kind="send",
        # 每次点击各回一条：按入站事件去重，重试不会重复发。
        dedupe_key=f"{inbound.id}:live_progress",
        target={"chat_id": inbound.chat_id, "message_id": card_message},
        payload={"text": text, "_reply": True},
    )
    return result
