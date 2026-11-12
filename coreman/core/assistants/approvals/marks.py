"""「处理中」表情：助理唯一不经审批的写操作。

表情类型固定；目标只能是别人发来、所有者看得到、还没被撤回的消息。平台自己记下表情的编号，
撤销时只能撤自己打的那一个。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import write_fuse
from coreman.core.assistants.approvals import checks
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantMark
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Call

# 飞书表情「我在处理」的类型名。
PROCESSING_EMOJI = "OnIt"
MAX_MARKS = 20


async def mark(
    session: AsyncSession, cipher: Cipher, scope: Scope, message_id: str, *, done: bool
) -> dict[str, Any]:
    call = Call(session, cipher, scope)
    bot_id = scope.bot.id
    row = await session.get(AssistantMark, (bot_id, message_id), with_for_update=True)
    if done:
        if row is None:
            return {"status": "not_marked"}
        with write_fuse.allow(bot_id):
            await call(
                "im.reaction_delete",
                path={"message_id": message_id, "reaction_id": row.reaction_id},
            )
        await session.delete(row)
        return {"status": "unmarked"}
    if row is not None:
        return {"status": "marked"}
    found = await checks.message(call, message_id)
    if (
        found is None
        or found.deleted
        or found.sender_type != "user"
        or found.sender_id == scope.open_id
    ):
        return {"error": "not_a_pending_message", "hint": "只能标记别人发给本人、未撤回的消息。"}
    count = await session.scalar(
        select(func.count()).select_from(AssistantMark).where(AssistantMark.bot_id == bot_id)
    )
    if (count or 0) >= MAX_MARKS:
        return {"error": "too_many_marks", "limit": MAX_MARKS}
    with write_fuse.allow(bot_id):
        data = await call(
            "im.reaction",
            path={"message_id": message_id},
            json={"reaction_type": {"emoji_type": PROCESSING_EMOJI}},
        )
    reaction_id = str(data.get("reaction_id") or "")
    if not reaction_id:
        # 没有编号就撤销不掉，还会占满名额：不记。
        return {"error": "mark_failed"}
    session.add(
        AssistantMark(
            bot_id=bot_id, message_id=message_id, chat_id=found.chat_id, reaction_id=reaction_id
        )
    )
    return {"status": "marked"}
