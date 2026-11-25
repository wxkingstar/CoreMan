"""任务平台卡片上的点击：回答待选择事项，和网页上回答走同一条路（先回答的生效）。

调用方（`card_actions`）已核对卡片是我们发给点击人私聊的。点击人按飞书 user_id 认成员，
回答的权限由 `service.answer` 再核对一遍。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.db.models import Bot, InboundEvent, User, UserIdentity, WorkTask
from coreman.core.work_tasks import cards, execute, flow
from coreman.core.work_tasks.service import WorkTaskError, current_line
from coreman.runtime.worker.context import TaskContext


async def _clicker(session: AsyncSession, inbound: InboundEvent) -> User | None:
    pid = str(inbound.sender_platform_user_id or "")
    if not pid:
        return None
    user_id = await session.scalar(
        select(UserIdentity.user_id).where(
            UserIdentity.platform == "feishu", UserIdentity.platform_user_id == pid
        )
    )
    user = await session.get(User, user_id) if user_id else None
    return user if user is not None and user.status == "active" else None


async def _show(
    session: AsyncSession, bot: Bot, inbound: InboundEvent, row: WorkTask, line: str, base: str
) -> None:
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{inbound.id}:card_update",
        target={"chat_id": inbound.chat_id, "message_id": inbound.reply_context.get("message_id")},
        payload={"card": cards.result_card(row, line, base)},
    )


async def handle(
    session: AsyncSession,
    ctx: TaskContext,
    bot: Bot,
    inbound: InboundEvent,
    action: dict[str, Any],
) -> str:
    ask_id, _, option = str(action.get("level") or "").partition(":")
    try:
        task_pk = int(str(action["task_id"]).split(":")[1])
    except (KeyError, ValueError, IndexError):
        return "ignored"
    row = await session.get(WorkTask, task_pk, with_for_update=True)
    user = await _clicker(session, inbound)
    if row is None or user is None or not ask_id or not option:
        return "ignored"
    selected = action.get("selected") or {}
    values = selected.get(cards.TEXT_FIELD) if isinstance(selected, dict) else None
    text = str(values[0]) if isinstance(values, list) and values else ""
    before_ask, before_status = row.ask, row.status
    try:
        await execute.answer(
            session, row, user, ask_id=ask_id, option=option, text=text, channel="feishu"
        )
    except WorkTaskError as exc:
        if exc.status == 422:
            # 退回、拒绝没写理由：卡片不动，让他补上再点（网页上同样拒绝）。
            return "needs_text"
        await _show(
            session,
            bot,
            inbound,
            row,
            f"{exc.message}。现在：{current_line(row, {})}",
            ctx.public_base_url,
        )
        return "stale"
    await session.flush()
    await flow.settle(
        session,
        row,
        before_ask=before_ask,
        before_status=before_status,
        actor_id=user.id,
        base_url=ctx.public_base_url,
    )
    if row.ask and before_ask and row.ask.get("id") == before_ask.get("id"):
        # 选了「其他」：话记下了，问题还在，卡片不动。
        return "noted"
    return "answered"
