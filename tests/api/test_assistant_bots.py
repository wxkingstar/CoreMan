"""助理记录在机器人接口上的可见性与锁定字段。"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.db.models import Bot, PlatformApp, User
from tests.api.conftest import login_as, login_existing


async def _assistant(db_session: AsyncSession, owner: User) -> Bot:
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    db_session.add(app)
    await db_session.flush()
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    db_session.add(bot)
    await db_session.commit()
    return bot


async def test_only_owner_sees_assistant(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    mine = (await client.get("/api/admin/bots", params={"scope": "mine"})).json()["data"]
    assert [b["id"] for b in mine["items"]] == [str(bot.id)]
    assert (await client.get(f"/api/admin/bots/{bot.id}")).status_code == 200

    await login_as(client, db_session, role="platform_admin")
    everything = (await client.get("/api/admin/bots", params={"scope": "all"})).json()["data"]
    assert str(bot.id) not in [b["id"] for b in everything["items"]]
    assert (await client.get(f"/api/admin/bots/{bot.id}")).status_code == 404


async def test_owner_cannot_change_locked_fields(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    headers = {"If-Match": f'"{bot.version}"'}
    r = await client.patch(
        f"/api/admin/bots/{bot.id}", json={"group_observe": "on"}, headers=headers
    )
    assert r.status_code == 422 and "group_observe" in r.text
    r = await client.patch(f"/api/admin/bots/{bot.id}", json={"dev_mode": True}, headers=headers)
    assert r.status_code == 422
    r = await client.patch(
        f"/api/admin/bots/{bot.id}", json={"description": "我的助理"}, headers=headers
    )
    assert r.status_code == 200, r.text


async def test_no_members_or_allowed_users(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    colleague = User(login_name="lisi", display_name="李四")
    db_session.add(colleague)
    await db_session.commit()
    await login_existing(client, db_session, owner)
    r = await client.post(f"/api/admin/bots/{bot.id}/members", json={"user_id": str(colleague.id)})
    assert r.status_code == 422
    r = await client.put(
        f"/api/admin/bots/{bot.id}/allowed-users", json={"user_ids": [str(colleague.id)]}
    )
    assert r.status_code == 422
