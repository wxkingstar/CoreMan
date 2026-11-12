"""话题选择卡片的点击：独立开启新会话，或者继承群主会话。

只认发这条消息的本人点；别人点了不回卡。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat import busy_choice, topics
from coreman.core.db.models import Bot, Task
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.context import TaskContext


async def handle(
    session: AsyncSession, ctx: TaskContext, bot: Bot, action: dict[str, Any], icon: str
) -> tuple[str, dict[str, Any] | None]:
    """返回 (结果, 要换上的卡片)；卡片为 None 表示不动原卡。"""
    card_task_id = str(action.get("task_id") or "")
    target_id = topics.parse_card_task_id(card_task_id)
    target = (
        await session.get(Task, target_id, populate_existing=True, with_for_update=True)
        if target_id
        else None
    )
    clicker = str(ctx.task.payload.get("platform_user_id") or "")
    if (
        target is None
        or target.bot_id != bot.id
        or target.kind != "chat"
        or not topics.is_topic(target.session_key)
        or topics.chat_of(str(target.session_key)) != str(ctx.task.payload.get("chat_id") or "")
        or not clicker
        or str(target.payload.get("platform_user_id") or "") != clicker
    ):
        ctx.log.info("topic_choice_ignored", target_task_id=target_id)
        return "ignored", None

    def card(title: str, desc: str) -> dict[str, Any]:
        return busy_choice.result_card(
            bot.platform,
            card_task_id,
            title=msg(title, ctx.locale),
            desc=msg(desc, ctx.locale),
            icon_url=icon,
            locale=ctx.locale,
        )

    if (
        target.status != "queued"
        or target.cancel_requested_at is not None
        or not target.payload.get("topic_hold")
    ):
        return "late", card("topic_late_title", "topic_late_desc")
    choice = str(action.get("event_key") or "")
    if choice not in (topics.NEW, topics.FORK):
        ctx.log.warning("topic_choice_unknown", choice=choice)
        return "ignored", None
    forked = await topics.choose(
        session, bot.id, str(target.session_key), fork=choice == topics.FORK
    )
    await topics.release(session, target)
    if choice == topics.NEW:
        return "new", card("topic_new_title", "topic_new_desc")
    if not forked:
        # 群主会话刚好过期被清掉了：只能从头聊。
        return "nomain", card("topic_new_title", "topic_nomain_desc")
    return "fork", card("topic_fork_title", "topic_fork_desc")
