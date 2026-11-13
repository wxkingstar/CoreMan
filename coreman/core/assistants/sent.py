"""平台经审批以所有者身份发出的消息的编号。

审批通过后由平台代发的消息，在飞书里发送人就是所有者。盯消息读到它时不能当作所有者亲自发言：
否则那个会话会被压 10 分钟，「所有者上次发言」被更新，同会话别的待批单还会被当成「所有者已
亲自回复」而自动撤回。这里只记消息编号（明文的定位信息，与「处理中」标记同一类），没有内容。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import AssistantSent

# 记录留这么久：盯消息的游标越过那条消息之后就用不上了，下次记的时候顺手清掉更早的。
KEEP = timedelta(days=30)


async def record(session: AsyncSession, bot_id: uuid.UUID, message_ids: Iterable[str]) -> None:
    """记下平台刚替这位所有者发出的消息。幂等；调用方提交。"""
    wanted = sorted({message_id for message_id in message_ids if message_id})
    if not wanted:
        return
    await session.execute(
        delete(AssistantSent).where(
            AssistantSent.bot_id == bot_id, AssistantSent.created_at < func.now() - KEEP
        )
    )
    await session.execute(
        insert(AssistantSent)
        .values([{"bot_id": bot_id, "message_id": message_id} for message_id in wanted])
        .on_conflict_do_nothing(index_elements=[AssistantSent.bot_id, AssistantSent.message_id])
    )


async def ours(session: AsyncSession, bot_id: uuid.UUID, message_id: str) -> bool:
    """这条消息是不是平台替这位所有者发的。"""
    found = await session.scalar(
        select(AssistantSent.message_id).where(
            AssistantSent.bot_id == bot_id, AssistantSent.message_id == message_id
        )
    )
    return found is not None
