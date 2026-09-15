"""command 任务处理器（reset / stop）。"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import tasks
from coreman.core.chat import interactions, sessions
from coreman.core.chat.announcements import find_announcement
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import Bot
from coreman.core.i18n.messages import msg
from coreman.runtime.worker.context import TaskContext
from coreman.runtime.worker.replies import load_inbound, reply_once


async def _speaker_id(
    session: AsyncSession, ctx: TaskContext, bot: Bot | None, platform_user_id: str
) -> str | None:
    """快车道也要先把发言者解析成明文 userid，再拿去清待答状态。

    待答状态的 scope 一律按 `resolve_speaker` 的结果建（`ChatTaskHandler._intake` 就是这么
    建的）。这里若直接用 payload 里的原始 id，同一个人走快车道 reset 与走整条流水线时会
    落在两个 scope 上——密文 open_userid 的用户就永远清不掉自己的待答状态。
    """
    if not platform_user_id or bot is None:
        return platform_user_id or None
    speaker = await resolve_speaker(
        session,
        platform=bot.platform,
        platform_user_id=platform_user_id,
        resolver=ctx.openuserid,
    )
    return speaker.platform_user_id or platform_user_id


async def _clear_states(
    session: AsyncSession, bot_id: uuid.UUID, session_key: str, platform_user_id: str | None
) -> int:
    """reset/stop 都作废待答状态：否则下一条输入会被吞成上一题的答案。"""
    closed = 0
    if platform_user_id:
        from sqlalchemy import or_, select

        from coreman.core.chat.bot_collaboration import ACTIVE, close
        from coreman.core.db.models import BotCollaboration, BotCollaborationRoute

        rows = list(
            await session.scalars(
                select(BotCollaboration)
                .join(BotCollaborationRoute)
                .where(
                    or_(
                        BotCollaborationRoute.source_bot_id == bot_id,
                        BotCollaborationRoute.target_bot_id == bot_id,
                    ),
                    BotCollaborationRoute.chat_id == session_key,
                    BotCollaboration.origin_platform_user_id == platform_user_id,
                    BotCollaboration.status.in_(ACTIVE),
                )
                .with_for_update(of=BotCollaboration)
            )
        )
        closed = len(rows)
        for row in rows:
            await close(session, row, "cancelled", "原始用户停止了协作")
        await interactions.clear_for_speaker(
            session, bot_id=bot_id, platform_user_id=platform_user_id
        )
    await interactions.clear_for_session(session, bot_id=bot_id, session_key=session_key)
    return closed


async def do_reset(
    session: AsyncSession,
    ctx: TaskContext,
    bot_id: uuid.UUID,
    session_key: str,
    platform_user_id: str | None = None,
) -> str:
    try:
        await _clear_states(session, bot_id, session_key, platform_user_id)
        await sessions.clear(session, bot_id, session_key)
    except Exception:  # noqa: BLE001
        ctx.log.warning("session_reset_failed")
        return msg("session_reset_failed", ctx.locale)
    return msg("session_reset_ok", ctx.locale)


async def do_stop(
    session: AsyncSession,
    ctx: TaskContext,
    bot_id: uuid.UUID,
    session_key: str,
    platform_user_id: str | None = None,
) -> str:
    stopped = await _clear_states(session, bot_id, session_key, platform_user_id)
    # Opt-in group tasks use per-request sessions, so stop follows original human provenance.
    if platform_user_id:
        from sqlalchemy import select

        from coreman.core.db.models import InboundEvent, Task

        scoped = list(
            await session.scalars(
                select(Task)
                .join(InboundEvent, Task.inbound_event_id == InboundEvent.id)
                .where(
                    Task.bot_id == bot_id,
                    Task.status.in_(tasks.OPEN),
                    Task.session_key.startswith(session_key + ":request:", autoescape=True),
                    InboundEvent.sender_platform_user_id == platform_user_id,
                    InboundEvent.chat_id == session_key,
                )
            )
        )
        for task in scoped:
            if task.id != ctx.task.id and await tasks.request_cancel(session, task.id, "user_stop"):
                stopped += 1
    for t in await tasks.active_for_session(session, bot_id, session_key):
        if t.id != ctx.task.id and await tasks.request_cancel(session, t.id, "user_stop"):
            stopped += 1
    return msg("stopped" if stopped else "nothing_running", ctx.locale)


class CommandHandler:
    kind = "command"

    async def run(self, ctx: TaskContext) -> None:
        async with ctx.session_factory() as session:
            inbound = await load_inbound(session, ctx.task)
            bot = await session.get(Bot, ctx.task.bot_id)
            command = str(ctx.task.payload.get("command", ""))
            key = ctx.task.session_key or inbound.chat_id
            # 公告拦在最前，和 chat 流水线同一个口径：维护期连 reset/stop 也只回公告。
            hit = (
                await find_announcement(session, bot_id=bot.id, relay_server_id=bot.relay_server_id)
                if bot is not None
                else None
            )
            if hit is not None:
                ctx.log.info("announcement_intercepted", announcement_id=str(hit.id))
                await reply_once(
                    session, ctx, reply_context=inbound.reply_context, text=hit.content
                )
                await tasks.finish(
                    session, ctx.task.id, status="succeeded", result={"announcement": True}
                )
                await session.commit()
                return
            pid = await _speaker_id(
                session, ctx, bot, str(ctx.task.payload.get("platform_user_id") or "")
            )
            if command == "reset":
                text = await do_reset(session, ctx, ctx.task.bot_id, key, pid)
            elif command == "stop":
                text = await do_stop(session, ctx, ctx.task.bot_id, key, pid)
            else:
                text = msg("help", ctx.locale)
            await reply_once(session, ctx, reply_context=inbound.reply_context, text=text)
            await tasks.finish(session, ctx.task.id, status="succeeded")
            await session.commit()
