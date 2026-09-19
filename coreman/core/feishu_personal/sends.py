"""Deduplicate mail sent as the owner: a retry with the same uuid never sends a second mail.

The record lives in the MCP request's transaction, which holds the owner's advisory lock
(taken again here) until it commits, so concurrent retries see each other's result. The
router commits after a Feishu error too, so a draft created before a failed send is reused.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import FeishuPersonalSend
from coreman.core.feishu_personal import service
from coreman.core.feishu_personal.policy import Scope

# Model retries happen within a turn; a week also covers a scheduled task's next run.
KEEP = timedelta(days=7)


async def claim(
    session: AsyncSession, scope: Scope, key: str, fingerprint: str
) -> FeishuPersonalSend:
    """This owner's record for `key`, created as `drafted` without a draft if new."""
    await service.lock(session, scope.bot.id, scope.user_id)
    owner = (
        FeishuPersonalSend.bot_id == scope.bot.id,
        FeishuPersonalSend.user_id == scope.user_id,
    )
    await session.execute(
        delete(FeishuPersonalSend).where(
            *owner, FeishuPersonalSend.created_at < datetime.now(UTC) - KEEP
        )
    )
    await session.execute(
        insert(FeishuPersonalSend)
        .values(
            bot_id=scope.bot.id,
            user_id=scope.user_id,
            send_key=key,
            fingerprint=fingerprint,
            status="drafted",
        )
        .on_conflict_do_nothing()
    )
    row = (
        await session.scalars(
            select(FeishuPersonalSend)
            .where(*owner, FeishuPersonalSend.send_key == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one()
    if row.fingerprint != fingerprint:
        # Same uuid, different mail: a model mistake, never a retry.
        raise service.PersonalError("uuid_reused")
    return row
