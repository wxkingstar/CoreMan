"""个人助理的知识库注入：主会话的首轮给置顶页与索引，之后只在置顶页有更新时补一条说明。

内容放进本轮块（随用户消息经标准输入传给 CLI），不进 system prompt：命令行里没有个人内容，
稳定段逐字不变。会话每天换新，所以每天完整给一次。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import notes
from coreman.core.assistants.records import is_assistant
from coreman.core.chat import sessions
from coreman.core.crypto import DecryptError
from coreman.core.db.models import ChatLog
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext


async def _previous_turn_at(
    session: AsyncSession, ctx: TaskContext, intake: Intake, info: sessions.SessionInfo
) -> datetime | None:
    """同一个 relay 会话里上一轮开始的时刻（任务的认领时刻，数据库时钟）；没有记录返回 None。"""
    at: datetime | None = await session.scalar(
        select(func.max(ChatLog.request_at)).where(
            ChatLog.bot_id == intake.bot.id,
            ChatLog.session_key == intake.session_key,
            ChatLog.relay_session_id == info.relay_session_id,
            ChatLog.task_id.is_distinct_from(ctx.task.id),
        )
    )
    return at


async def turn_note(
    session: AsyncSession, ctx: TaskContext, intake: Intake, info: sessions.SessionInfo
) -> str | None:
    """本轮要放进本轮块的知识库内容；不该注入或没有内容时返回 None。

    Args:
        info: `transcripts.prepare` 之后本轮实际用的会话
    """
    bot = intake.bot
    # 只管主会话（会话键就是所有者与助理的私聊）；子任务会话自己用工具读。
    if not is_assistant(bot) or intake.session_key != intake.chat_id:
        return None
    try:
        # 节点上没有这个会话的上文：新会话，或换运行时后的首轮。
        if info.is_new or info.inherited_from is not None:
            return await notes.pinned_block(session, bot.id)
        since = await _previous_turn_at(session, ctx, intake, info)
        if since is None:
            return await notes.pinned_block(session, bot.id)
        return await notes.changes_since(session, bot.id, since)
    except DecryptError:
        # 主密钥配错或密文被改：知识库读不出来不该让这一轮跑不了。不带路径与异常文字。
        ctx.log.warning("assistant_notes_unreadable", bot_id=str(bot.id))
        return None
