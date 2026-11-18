"""所有者在管理台给自己的助理勾选业务系统：缺口记为解决，被卡住的事入一条续接提醒。

同时钉住一个事实：个人助理的创建者就是所有者，他是这位助理的机器人管理员，能自己改系统权限。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, gaps
from coreman.core.db.models import AssistantInbox, BusinessSystem
from tests.api.conftest import login_existing
from tests.integration.job_helpers import owner_with_key

pytestmark = pytest.mark.usefixtures("assistant_master")


async def _setup(db_session: AsyncSession, seen_rank: int):
    bot, owner = await owner_with_key(db_session)
    db_session.add(
        BusinessSystem(
            key="orders",
            name="订单系统",
            allowed_bot_ids=None,
            token_delivery="proxy",
            openapi_url="https://example.com/openapi.json",
        )
    )
    await followups.add(db_session, bot.id, category="promised", title="查订单")
    await gaps.report(
        db_session,
        bot.id,
        kind="business_system",
        key="orders",
        need="查订单",
        followup_seqs=[1],
        seen_rank=seen_rank,
    )
    bot_id, version = bot.id, bot.version
    await db_session.commit()
    return bot_id, version, owner


async def _put(client: httpx.AsyncClient, bot_id, version: int, body: dict) -> httpx.Response:
    return await client.put(
        f"/api/admin/bots/{bot_id}/system-grants",
        json=body,
        headers={"If-Match": str(version)},
    )


async def test_owner_grant_resolves_the_gap(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot_id, version, owner = await _setup(db_session, 0)
    await login_existing(client, db_session, owner)
    r = await _put(client, bot_id, version, {"system_keys": ["orders"]})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert (await gaps.get(db_session, bot_id, 1)).status == "resolved"
    kinds = list(
        await db_session.scalars(select(AssistantInbox.kind).where(AssistantInbox.bot_id == bot_id))
    )
    assert kinds == ["reminder"]


async def test_read_only_grant_does_not_resolve_a_read_level_gap(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot_id, version, owner = await _setup(db_session, 1)
    await login_existing(client, db_session, owner)
    r = await _put(client, bot_id, version, {"system_keys": ["orders"], "write_keys": []})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert (await gaps.get(db_session, bot_id, 1)).status == "open"
