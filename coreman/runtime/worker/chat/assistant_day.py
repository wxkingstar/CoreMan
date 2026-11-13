"""个人助理的主会话每天换新：按所有者时区的自然日，跨天的连续性靠待跟进与知识库。

在开轮取会话之后、定转录交接之前调用。换新就是给会话键换一个新的 relay 会话：之后这一轮
不还原转录，知识库的首轮注入重新给出置顶页与索引。子任务会话（键里有 `#job:`）不看。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import platform_turns
from coreman.core.assistants import settings as assistant_settings
from coreman.core.chat import sessions
from coreman.core.db.models import Bot
from coreman.core.timeutils import utcnow


async def roll(
    session: AsyncSession,
    bot: Bot,
    info: sessions.SessionInfo,
    *,
    session_key: str,
    chat_id: str,
    backend: str,
    speaker_user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> sessions.SessionInfo:
    """本轮实际用的会话：主会话是昨天或更早开始的就换一个新的。调用方提交。

    Args:
        info: `sessions.get_or_create` 刚给出的会话
        now: 用例注入的当前时刻；默认现在
    """
    if session_key != chat_id or platform_turns.JOB_SEPARATOR in session_key:
        return info
    prefs = await assistant_settings.get(session, bot.id)
    today = assistant_settings.local_today(prefs, now or utcnow())
    started = (
        None
        if info.is_new
        else await sessions.day_of(session, bot_id=bot.id, session_key=session_key)
    )
    if started is not None and started >= today:
        return info
    if started is not None:
        # 昨天或更早开始的会话：换新。不带续接——跨天不延续上下文。
        info = await sessions.renew(
            session,
            bot_id=bot.id,
            session_key=session_key,
            backend=backend,
            speaker_user_id=speaker_user_id,
        )
    # 新会话、刚换的会话、升级前就有而没记过日期的会话：都从今天算起。
    await sessions.stamp_day(session, bot_id=bot.id, session_key=session_key, day=today)
    return info
