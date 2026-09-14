"""即时回复与流打开的公共件。"""

from __future__ import annotations

import random
import string
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot, BotLease, InboundEvent, Task, TaskStream
from coreman.runtime.bus import streams
from coreman.runtime.worker.context import TaskContext

_ALNUM = string.ascii_letters + string.digits


def stream_id_for(bot_key: str, platform_user_id: str, now: float | None = None) -> str:
    ts = int(now if now is not None else time.time())
    rnd = "".join(random.choices(_ALNUM, k=6))
    return f"bot:{bot_key}|user:{platform_user_id}|ts:{ts}|rnd:{rnd}"


async def lease_generation(session: AsyncSession, bot_id: uuid.UUID) -> int:
    lease = await session.get(BotLease, bot_id)
    return int(lease.generation) if lease else 0


async def load_inbound(session: AsyncSession, task: Task) -> InboundEvent:
    if task.inbound_event_id is None:
        raise ValueError("任务没有关联入站事件")
    ev = await session.get(InboundEvent, task.inbound_event_id)
    if ev is None:
        raise ValueError("入站事件不存在")
    return ev


async def open_stream(
    session: AsyncSession,
    ctx: TaskContext,
    *,
    reply_context: dict[str, Any],
    session_url: str | None = None,
    stream_id: str | None = None,
    platform: str | None = None,
    delivery_mode: str = "stream",
    background_state: dict[str, Any] | None = None,
) -> TaskStream:
    """开流。`delivery_mode='proactive'` + `background_state` = 这条流从创建就归 outbox 推。"""
    if platform is None:
        bot = await session.get(Bot, ctx.task.bot_id)
        if bot is None:
            raise ValueError("机器人不存在")
        platform = bot.platform
    sid = stream_id or stream_id_for(
        str(ctx.task.payload.get("bot_key", "")), str(ctx.task.payload.get("platform_user_id", ""))
    )
    ctx.bind_stream(sid)
    return await streams.create(
        session,
        task_id=ctx.task.id,
        bot_id=ctx.task.bot_id,
        platform=platform,
        stream_id=sid,
        reply_context=reply_context,
        lease_generation=await lease_generation(session, ctx.task.bot_id),
        running_since=datetime.now(UTC),
        session_url=session_url,
        delivery_mode=delivery_mode,
        background_state=background_state,
    )


async def reply_once(
    session: AsyncSession, ctx: TaskContext, *, reply_context: dict[str, Any], text: str
) -> None:
    await open_stream(session, ctx, reply_context=reply_context)
    await streams.complete(session, ctx.task.id, final_text=text)
