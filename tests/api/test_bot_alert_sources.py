"""告警来源管理接口：只有平台管理角色能绑定服务用户。"""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import User
from tests.api.conftest import login_as
from tests.integration.worker_helpers import seed_bot


async def _bot(db_session: AsyncSession):
    bot, _, _ = await seed_bot(db_session)
    bot.platform = "feishu"
    await db_session.commit()
    return bot


async def test_only_manager_can_manage(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    await login_as(client, db_session, role="member")
    url = f"/api/admin/bots/{bot.id}/alert-sources"
    assert (await client.get(url)).status_code == 403


async def test_crud_and_validation(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    bot = await _bot(db_session)
    await login_as(client, db_session, role="platform_admin")
    service = User(login_name="svc-demo", display_name="服务账号", email="svc-demo@example.test")
    stopped = User(login_name="svc-off", display_name="停用", status="disabled")
    db_session.add_all([service, stopped])
    await db_session.commit()
    url = f"/api/admin/bots/{bot.id}/alert-sources"
    body = {"chat_id": "oc_x", "sender_union_id": "on_x", "service_user_id": str(service.id)}
    bad = await client.post(url, json={**body, "service_user_id": str(stopped.id)})
    assert bad.status_code == 422
    created = await client.post(url, json=body)
    assert created.status_code == 200
    source = created.json()["data"]
    assert source["enabled"] is True and source["dedupe_seconds"] == 600
    assert (await client.post(url, json=body)).status_code == 409
    patched = await client.patch(f"{url}/{source['id']}", json={"enabled": False})
    assert patched.json()["data"]["enabled"] is False
    assert len((await client.get(url)).json()["data"]["items"]) == 1
    assert (await client.delete(f"{url}/{source['id']}")).status_code == 200
    assert (await client.get(url)).json()["data"]["items"] == []
