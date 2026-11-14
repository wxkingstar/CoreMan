"""「处理中」表情：助理唯一不经审批的写操作。

表情类型固定；目标只能是别人发来、所有者看得到、还没被撤回的消息。平台自己记下表情的编号，
撤销时只能撤自己打的那一个。

只在需要调查的事上打，调查一结束就撤。平台兜底（`release_due`）：打上它的那一轮结束时撤掉还
挂着的；那一轮派出的子任务或调度接着查的（`bind_job`），等它的结论交回的那一轮结束再撤。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed, write_fuse
from coreman.core.assistants.approvals import checks
from coreman.core.bus import tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantJob, AssistantMark, Task
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.logging import get_logger

log = get_logger(__name__)

# 飞书表情「我在处理」的类型名。
PROCESSING_EMOJI = "OnIt"
MAX_MARKS = 20
# 一次兜底最多撤这么多个：每个都是一次以所有者身份的飞书调用。
RELEASE_BATCH = 20


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
            bot_id=bot_id,
            message_id=message_id,
            chat_id=found.chat_id,
            reaction_id=reaction_id,
            task_id=scope.task.id if scope.task is not None else None,
        )
    )
    return {"status": "marked"}


async def bind_job(
    session: AsyncSession, bot_id: uuid.UUID, task_id: int, job_id: uuid.UUID
) -> int:
    """这一轮把调查交给了子任务或调度：它打上的表情等那件事的结论交回之后再撤。只 flush。"""
    done = await session.execute(
        update(AssistantMark)
        .where(AssistantMark.bot_id == bot_id, AssistantMark.task_id == task_id)
        .values(job_id=job_id)
    )
    return int(getattr(done, "rowcount", 0) or 0)


async def due(session: AsyncSession, bot_id: uuid.UUID) -> list[AssistantMark]:
    """该由平台撤掉的表情：打上它的那一轮已经结束（任务行不在了也算），且没有交给别的工作，
    或交给的子任务、调度已经把结论交回（或不需要交回、交回不了）、已经被删掉。

    升级前打的（没有记那一轮）不在里面，仍按时间提醒。
    """
    found = await session.scalars(
        select(AssistantMark)
        .outerjoin(Task, Task.id == AssistantMark.task_id)
        .outerjoin(AssistantJob, AssistantJob.id == AssistantMark.job_id)
        .where(
            AssistantMark.bot_id == bot_id,
            AssistantMark.task_id.is_not(None),
            or_(Task.id.is_(None), Task.status.not_in(tasks.OPEN)),
            or_(AssistantMark.job_id.is_(None), AssistantJob.reported_at.is_not(None)),
        )
        .order_by(AssistantMark.created_at)
        .limit(RELEASE_BATCH)
        .with_for_update(of=AssistantMark, skip_locked=True)
    )
    return list(found)


async def release_due(session: AsyncSession, call: checks.Reader, bot_id: uuid.UUID) -> int:
    """撤掉该撤的表情，返回撤了几个。调用方提交（调过飞书之后不回滚：刷新的令牌要落库）。

    撤不掉的（飞书报错）留着下次再试，只记错误代码；一个失败不挡后面的。
    """
    released = 0
    for row in await due(session, bot_id):
        try:
            with write_fuse.allow(bot_id):
                await call(
                    "im.reaction_delete",
                    path={"message_id": row.message_id, "reaction_id": row.reaction_id},
                )
        except PersonalError as exc:
            log.warning(
                "assistant_mark_release_failed", bot_id=str(bot_id), code=sealed.code_only(exc.code)
            )
            continue
        await session.delete(row)
        released += 1
    return released
