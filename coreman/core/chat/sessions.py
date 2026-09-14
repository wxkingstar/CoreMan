"""chat_sessions（spec §8.5）：只管 relay_session_id，对话历史由 relay 按 session_id 自持。

所以「清会话」= 删掉这里的映射行，下一轮自然换一个新的 relay 会话；不需要、也无法去 relay
上删历史。换机、换模型、超 TTL 都走这条路。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Delete, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import ChatSession


@dataclass(frozen=True)
class SessionInfo:
    """本轮用哪个 relay 会话，以及要不要在 system prompt 里加「新会话 / 换人」提示。"""

    relay_session_id: uuid.UUID
    is_new: bool
    speaker_changed: bool


async def get_or_create(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    session_key: str,
    backend: str,
    ttl_hours: int,
    speaker_user_id: uuid.UUID | None,
) -> SessionInfo:
    """取（或新建）本轮的 relay 会话，并顺手把活跃时间与发言者刷成本轮的值。

    无记录 / 超 TTL / backend 变了（claude ↔ codex）都算「新会话」：旧的 relay 会话 id 在
    新后端上根本不存在，必须换一个。upsert 立即落库（同调用方事务），这样即便本轮对话后面
    炸了，下一轮也不会拿到一个 relay 侧从没建过的会话 id。
    """
    row = await session.get(ChatSession, (bot_id, session_key), populate_existing=True)
    now = datetime.now(UTC)
    fresh = (
        row is not None
        and row.backend == backend
        and row.last_active_at >= now - timedelta(hours=ttl_hours)
    )
    # 只有「续用旧会话 + 两边都认得出人」才算换人：匿名 → 具名不是换人，是刚认出来。
    speaker_changed = bool(
        fresh
        and row is not None
        and row.last_speaker_user_id is not None
        and speaker_user_id is not None
        and row.last_speaker_user_id != speaker_user_id
    )
    sid = row.relay_session_id if (fresh and row is not None) else uuid.uuid4()
    stmt = insert(ChatSession).values(
        bot_id=bot_id,
        session_key=session_key,
        relay_session_id=sid,
        backend=backend,
        last_speaker_user_id=speaker_user_id,
        last_active_at=func.now(),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[ChatSession.bot_id, ChatSession.session_key],
            set_={
                "relay_session_id": sid,
                "backend": backend,
                "last_speaker_user_id": speaker_user_id,
                "last_active_at": func.now(),
            },
        )
    )
    return SessionInfo(sid, not fresh, speaker_changed)


async def switch_to(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    session_key: str,
    relay_session_id: uuid.UUID,
    backend: str,
    speaker_user_id: uuid.UUID | None,
) -> None:
    """把这个会话键指向一个历史 relay 会话（sessions 命令的序号切换）。

    同时刷 `last_active_at`：刚切过去的会话要是还挂着几天前的活跃时间，下一条消息就会被
    TTL 判成过期，用户刚切完又被弹回一个新会话。
    """
    stmt = insert(ChatSession).values(
        bot_id=bot_id,
        session_key=session_key,
        relay_session_id=relay_session_id,
        backend=backend,
        last_speaker_user_id=speaker_user_id,
        last_active_at=func.now(),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[ChatSession.bot_id, ChatSession.session_key],
            set_={
                "relay_session_id": relay_session_id,
                "backend": backend,
                "last_speaker_user_id": speaker_user_id,
                "last_active_at": func.now(),
            },
        )
    )


async def _deleted(session: AsyncSession, stmt: Delete) -> int:
    """跑一条 DELETE，返回删掉几行。

    `AsyncSession.execute` 的静态返回类型是 `Result`，只有 DML 实际拿到的 `CursorResult`
    才有 `rowcount`——这里把这处收窄集中掉，别让每个调用点各写一遍 cast。
    """
    res = cast(CursorResult[Any], await session.execute(stmt))
    return int(res.rowcount or 0)


async def clear(session: AsyncSession, bot_id: uuid.UUID, session_key: str) -> bool:
    """清掉一个会话（/clear 命令）。返回「本来有没有」，好给用户区分提示。"""
    stmt = delete(ChatSession).where(
        ChatSession.bot_id == bot_id, ChatSession.session_key == session_key
    )
    return bool(await _deleted(session, stmt))


async def clear_bot(session: AsyncSession, bot_id: uuid.UUID) -> int:
    """清掉这个机器人的所有会话（换机 / 换模型 / 换工作目录），返回清掉几个。"""
    return await _deleted(session, delete(ChatSession).where(ChatSession.bot_id == bot_id))


async def list_for_bot(session: AsyncSession, bot_id: uuid.UUID) -> list[ChatSession]:
    return list(
        (await session.execute(select(ChatSession).where(ChatSession.bot_id == bot_id))).scalars()
    )
