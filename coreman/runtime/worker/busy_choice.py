"""「上一条还在处理」卡片的点击：等它做完再做这条、停下上一条先做这条、看看上一条的进度，或者放弃这条。

只认发这条消息的本人点；别人点了不回卡（回了就等于替他把卡改掉）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.chat import busy_choice
from coreman.core.db.models import Bot, Task
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.chat.queued_batch import MERGED
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

    if target.error_code == MERGED:
        return "late", card("late")
    if target.cancel_requested_at is not None or target.status in ("cancelled", "failed"):
        return "gone", card("gone")
    if target.status != "queued":
        return "late", card("late")
    choice = str(action.get("event_key") or "")
    if target.payload.get("stale_hold"):
        if choice == busy_choice.DROP:
            await tasks.finish(session, target.id, status="cancelled", error_code="user_dropped")
            return "drop", card("dropped")
        if choice in (busy_choice.RESUME, busy_choice.WAIT, busy_choice.STOP):
            await tasks.release_hold(session, target)
            return "resume", card("resumed")
        return "ignored", None
    if choice in (busy_choice.RESUME, busy_choice.DROP):
        return "late", card("late")
    if choice == busy_choice.CANCEL:
        await tasks.finish(session, target.id, status="cancelled", error_code="user_dropped")
        return "drop", card("dropped")
    if choice == busy_choice.WAIT:
        # 明确说了要等：轮到时不再问一次。
        target.payload = {**target.payload, "busy_wait": True}
        return "wait", card("wait", msg("busy_wait_summary", ctx.locale))
    if choice == busy_choice.PROGRESS:
        progress = await busy_choice.progress_of(
            session,
            bot.id,
            str(target.session_key),
            clicker,
            before_task_id=target.id,
            locale=ctx.locale,
        )
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
