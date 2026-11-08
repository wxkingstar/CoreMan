"""「上一条还在处理」卡片的点击：停下上一条先做这条、等它做完，或者看看上一条的进度。

只认发这条消息的本人点；别人点了不回卡（回了就等于替他把卡改掉）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.chat import busy_choice
from coreman.core.db.models import Bot, Task, TaskStream
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.context import TaskContext


async def handle(
    session: AsyncSession, ctx: TaskContext, bot: Bot, action: dict[str, Any], icon: str
) -> tuple[str, dict[str, Any] | None]:
    """返回 (结果, 要换上的卡片)；卡片为 None 表示不动原卡。"""
    card_task_id = str(action.get("task_id") or "")
    target_id = busy_choice.parse_card_task_id(card_task_id)
    target = await session.get(Task, target_id, populate_existing=True) if target_id else None
    clicker = str(ctx.task.payload.get("platform_user_id") or "")
    if (
        target is None
        or target.bot_id != bot.id
        or target.kind != "chat"
        or target.session_key != str(ctx.task.payload.get("chat_id") or "")
        or not clicker
        or str(target.payload.get("platform_user_id") or "") != clicker
    ):
        ctx.log.info("busy_choice_ignored", target_task_id=target_id)
        return "ignored", None

    def card(key: str, summary: str = "") -> dict[str, Any]:
        return busy_choice.result_card(
            bot.platform,
            card_task_id,
            title=msg(f"busy_{key}_title", ctx.locale),
            desc=msg(f"busy_{key}_desc", ctx.locale),
            summary=summary,
            icon_url=icon,
            locale=ctx.locale,
        )

    if target.cancel_requested_at is not None or target.status in ("cancelled", "failed"):
        return "gone", card("gone")
    if target.status != "queued":
        return "late", card("late")
    choice = str(action.get("event_key") or "")
    if choice == busy_choice.WAIT:
        return "wait", card("wait", msg("busy_wait_summary", ctx.locale))
    if choice == busy_choice.PROGRESS:
        progress = await _progress(session, bot, target, clicker, ctx.locale)
        return "progress", busy_choice.offer_card(
            bot.platform, target.id, progress=progress, locale=ctx.locale
        )
    if choice != busy_choice.STOP:
        ctx.log.warning("busy_choice_unknown", choice=choice)
        return "ignored", None
    # 只停这个人自己排在前面的消息；群里别人交代的事照样做完。
    ahead = await session.execute(
        select(Task.id, Task.status).where(
            Task.bot_id == bot.id,
            Task.session_key == target.session_key,
            Task.kind == "chat",
            Task.status.in_(tasks.OPEN),
            Task.id < target.id,
            Task.payload["platform_user_id"].astext == clicker,
        )
    )
    started = False
    for tid, status in list(ahead):
        if await tasks.request_cancel(session, tid, "user_stop"):
            started = started or status in tasks.ACTIVE
    if started:
        # 停下的是已经开跑的：新一轮的本轮块据此告诉模型上一条做到一半被停了，别接着做它。
        target.payload = {**target.payload, "stopped_previous": True}
    return "stop", card("stopped")


async def _progress(
    session: AsyncSession, bot: Bot, target: Task, clicker: str, locale: str
) -> str:
    """这个人排在前面的那条（优先正在跑的）现在到哪了。"""
    rows = list(
        await session.scalars(
            select(Task)
            .where(
                Task.bot_id == bot.id,
                Task.session_key == target.session_key,
                Task.kind == "chat",
                Task.status.in_(tasks.OPEN),
                Task.cancel_requested_at.is_(None),
                Task.id < target.id,
                Task.payload["platform_user_id"].astext == clicker,
            )
            .order_by(Task.id)
        )
    )
    if not rows:
        return msg("busy_progress_done", locale)
    ahead = next((row for row in rows if row.status in tasks.ACTIVE), rows[0])
    stream = await session.get(TaskStream, ahead.id, populate_existing=True)
    since = (stream.running_since if stream else None) or ahead.started_at or ahead.claimed_at
    seconds = (datetime.now(UTC) - since).total_seconds() if since else 0.0
    return busy_choice.progress_text(
        status=ahead.status,
        seconds=seconds,
        thinking_md=stream.thinking_md if stream else "",
        reply_text=stream.pending_text if stream else "",
        locale=locale,
    )
