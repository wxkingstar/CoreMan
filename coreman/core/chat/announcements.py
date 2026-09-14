"""公告匹配（spec §8.2 步骤 2）：bot > relay > global，启用且在时间窗内，同级取最新。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Announcement


async def find_announcement(
    session: AsyncSession,
    *,
    bot_id: uuid.UUID,
    relay_server_id: uuid.UUID | None,
    now: datetime | None = None,
) -> Announcement | None:
    """命中的那条公告；没有就 None。

    三级依次试，先命中先返回：bot 级最贴身，relay 级覆盖一批机器人，global 级兜底。
    同级按 `created_at` 取最新——运营改口径时直接新发一条，不必先去停用旧的。
    """
    now = now or datetime.now(UTC)
    window = (
        Announcement.is_active.is_(True),
        or_(Announcement.start_at.is_(None), Announcement.start_at <= now),
        or_(Announcement.end_at.is_(None), Announcement.end_at >= now),
    )
    targets = [Announcement.bot_id == bot_id]
    if relay_server_id is not None:
        targets.append(Announcement.relay_server_id == relay_server_id)
    targets.append(Announcement.scope == "global")
    for target in targets:
        stmt = (
            select(Announcement)
            .where(target, *window)
            .order_by(Announcement.created_at.desc(), Announcement.id.desc())
            .limit(1)
        )
        hit = (await session.execute(stmt)).scalar_one_or_none()
        if hit is not None:
            return hit
    return None
