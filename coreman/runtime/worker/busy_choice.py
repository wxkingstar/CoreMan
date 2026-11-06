"""「上一条还在处理」卡片的点击：停下上一条先做这条，或者等它做完。

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

    def card(key: str) -> dict[str, Any]:
        return busy_choice.result_card(
            bot.platform,
            card_task_id,
            title=msg(f"busy_{key}_title", ctx.locale),
            desc=msg(f"busy_{key}_desc", ctx.locale),
            icon_url=icon,
            locale=ctx.locale,
        )

    if target.cancel_requested_at is not None or target.status in ("cancelled", "failed"):
        return "gone", card("gone")
    if target.status != "queued":
        return "late", card("late")
    choice = str(action.get("event_key") or "")
    if choice == busy_choice.WAIT:
        return "wait", card("wait")
    if choice != busy_choice.STOP:
        ctx.log.warning("busy_choice_unknown", choice=choice)
        return "ignored", None
    # 只停这个人自己排在前面的消息；群里别人交代的事照样做完。
    ahead = await session.scalars(
        select(Task.id).where(
            Task.bot_id == bot.id,
            Task.session_key == target.session_key,
            Task.kind == "chat",
            Task.status.in_(tasks.OPEN),
            Task.id < target.id,
            Task.payload["platform_user_id"].astext == clicker,
        )
    )
    for task_id in list(ahead):
        await tasks.request_cancel(session, task_id, "user_stop")
    return "stop", card("stopped")
