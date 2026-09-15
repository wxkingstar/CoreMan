"""Resolve display names without changing identifiers or visibility filters."""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Bot


async def bot_names(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = set(ids)
    if not ids:
        return {}
    rows = await session.execute(select(Bot.id, Bot.name).where(Bot.id.in_(ids)))
    return {row[0]: row[1] for row in rows.all()}
