"""换运行时后续上下文：原会话带不过去时，新会话首轮的本轮块里附上原会话最近的对话。

对话取自 chat_logs（每轮都记了用户原话、回复与当时的 relay_session_id），按时间顺序排，
从最近的往前取，超出总长度就丢最早的。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import ChatLog

MAX_TURNS = 20
MAX_MESSAGE = 1500
MAX_REPLY = 3000
MAX_TOTAL = 20000

HEADER = (
    "- 上下文续接：本会话刚从另一台运行时切换过来，原会话没能直接带过来。下面是原会话最近的"
    "对话（按时间顺序，可能有截断），请据此接着当前话题，不要向用户复述或提及这段记录。"
)


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


async def carried_context(
    session: AsyncSession, *, bot_id: uuid.UUID, session_key: str, relay_session_id: uuid.UUID
) -> str | None:
    rows = (
        await session.scalars(
            select(ChatLog)
            .where(
                ChatLog.bot_id == bot_id,
                ChatLog.session_key == session_key,
                ChatLog.relay_session_id == relay_session_id,
                ChatLog.status.in_(("success", "ask_user")),
            )
            .order_by(ChatLog.request_at.desc())
            .limit(MAX_TURNS)
        )
    ).all()
    turns: list[str] = []
    total = 0
    for row in rows:
        speaker = row.user_name or row.user_login or "用户"
        parts = []
        if row.message_content:
            parts.append(f"{speaker}：{clip(row.message_content, MAX_MESSAGE)}")
        if row.response_content:
            parts.append(f"你：{clip(row.response_content, MAX_REPLY)}")
        if not parts:
            continue
        turn = "\n".join(parts)
        if total + len(turn) > MAX_TOTAL:
            break
        total += len(turn)
        turns.append(turn)
    if not turns:
        return None
    body = "\n\n".join(reversed(turns))
    return f"{HEADER}\n<previous_conversation>\n{body}\n</previous_conversation>"
