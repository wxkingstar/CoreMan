"""流式卡片上「停止」按钮的点击：核对后停掉这一条回复。

只认这一轮的提问人（群里别人点了不算），并且点的必须是这条任务自己的流式卡片（按投递记录里
的消息 ID 核对，转发出去或伪造的回调停不了别人的任务）。停下后 worker 照常收尾：已经写出的
内容留在卡片里，末尾加「任务已被用户停止」。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox, tasks
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
    clicked = str(inbound.reply_context.get("message_id") or "")
    confirmed = action.get("level") == stream_stop.CONFIRMED
    if (
        target is None
        or target.bot_id != bot.id
        or target.kind != "chat"
        or delivery is None
        or not delivery.message_id
        or not (
            delivery.message_id == clicked
            or (confirmed and await _confirm_card_sent(session, bot, target.id, clicked))
        )
    ):
        ctx.log.info("stream_stop_ignored", target_task_id=target_id)
        return "ignored"
    if not clicker or str(target.payload.get("platform_user_id") or "") != clicker:
        return "not_requester"
    if target.status not in tasks.OPEN or target.cancel_requested_at is not None:
        return "late"
    if not confirmed and (running := await _live_running(session, target)):
        # 这一轮写在后台阶段的进程里：停下会连带停掉所有后台任务，先确认。
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"live-stop-confirm:{target.id}:{ctx.task.id}",
            target={"chat_id": inbound.chat_id, "message_id": delivery.message_id},
            payload={
                "card": stream_stop.confirm_card(target.id, running, ctx.locale),
                "_reply": True,
                "_live_stop_confirm": target.id,
            },
        )
        return "confirm"
    if not await tasks.request_cancel(session, target.id, "user_stop"):
        return "late"
    return "stopped"


async def _live_running(session: AsyncSession, target: Task) -> int:
    """目标这一轮写在后台阶段的进程里时，此刻还在跑的后台任务数；不是就是 0。"""
    from coreman.core.chat import background_runs
    from coreman.core.db.models import BackgroundRun

    run_id = target.payload.get("live_join")
    run = await session.get(BackgroundRun, int(run_id)) if run_id else None
    if run is None or run.state != "running":
        return 0
    return len(background_runs.running(run.tasks or []))


async def _confirm_card_sent(
    session: AsyncSession, bot: Bot, target_id: int, message_id: str
) -> bool:
    """点的是不是我们为这一轮发出的确认卡（按出站记录里的消息 ID 核对）。"""
    from sqlalchemy import select

    from coreman.core.db.models import OutboxItem

    found = await session.scalar(
        select(OutboxItem.id).where(
            OutboxItem.bot_id == bot.id,
            OutboxItem.status == "sent",
            OutboxItem.payload["_feishu_message_id"].astext == message_id,
            OutboxItem.payload["_live_stop_confirm"].astext == str(target_id),
        )
    )
    return bool(message_id) and found is not None
