"""apply_grants：与管理台 PUT 同一段写入；不开放的系统一律 403；写入的系统必须在授权里。"""

from __future__ import annotations

import pytest
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.errors import ApiError
from coreman.api.system_grants import apply_grants, current_grants
from coreman.core.db.models import AuditLog, BusinessSystem
from tests.integration.job_helpers import owner_with_key

pytestmark = pytest.mark.usefixtures("assistant_master")


def _request(app) -> Request:  # type: ignore[no-untyped-def]
    return Request({"type": "http", "app": app, "headers": [], "client": ("192.0.2.1", 1)})


async def test_apply_and_refuse(app, db_session: AsyncSession) -> None:  # type: ignore[no-untyped-def]
    bot, owner = await owner_with_key(db_session)
    db_session.add_all(
        [
            BusinessSystem(key="orders", name="订单系统", allowed_bot_ids=None),
            BusinessSystem(key="closed", name="限定的", allowed_bot_ids=[]),
        ]
    )
    await db_session.commit()
    version = bot.version
    keys, writes = await apply_grants(
        db_session, _request(app), owner, bot, keys=["orders"], write_keys=["orders"]
    )
    await db_session.commit()
    assert (keys, writes) == (["orders"], ["orders"])
    assert await current_grants(db_session, bot.id) == (["orders"], ["orders"])
    assert bot.version == version + 1
    actions = list(await db_session.scalars(select(AuditLog.action)))
    assert "bot.system_grants" in actions
    with pytest.raises(ApiError) as caught:
        await apply_grants(
            db_session, _request(app), owner, bot, keys=["orders", "closed"], write_keys=None
        )
    assert caught.value.status_code == 403
    await db_session.rollback()
