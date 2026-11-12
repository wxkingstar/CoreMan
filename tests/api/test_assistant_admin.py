"""管理员看个人助理与停用：只有元数据；停用只回状态；启停与删除不能绕过停用流程。"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.db.models import AssistantLifecycle, AuditLog, Bot, PlatformApp, User
from tests.api.conftest import login_as, login_existing
from tests.fakes.runtime_node import add_node_relay

SECRET = "机密-片段-7f3a"


async def _assistant(db_session: AsyncSession, owner: User, *, state: str = "active") -> Bot:
    app = PlatformApp(
        platform="feishu",
        name=f"个人助理-{uuid.uuid4().hex[:6]}",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    db_session.add(app)
    relay = await add_node_relay(db_session, name="pool-1")
    bot = new_assistant_bot(
        owner, app, relay_server_id=relay.id, model="m", working_dir="/w/a", credentials_enc=""
    )
    bot.assistant_state = state
    bot.description = SECRET
    db_session.add(bot)
    await db_session.commit()
    return bot


async def test_the_list_shows_who_where_and_what_state_only(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member", display_name="张三")
    bot = await _assistant(db_session, owner)
    assert (await client.get("/api/admin/assistants")).status_code == 403
    await login_as(client, db_session, role="platform_admin")
    response = await client.get("/api/admin/assistants")
    assert response.status_code == 200
    [item] = response.json()["data"]["items"]
    assert item["bot_id"] == str(bot.id) and item["state"] == "active"
    assert item["owner"]["display_name"] == "张三" and item["node"]["name"] == "pool-1"
    assert item["lifecycle"] is None and item["directories_pending_removal"] == 0
    assert SECRET not in response.text and "/w/a" not in response.text


async def test_an_admin_deprovision_returns_nothing_but_the_state(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    admin = await login_as(client, db_session, role="platform_admin")
    url = f"/api/admin/assistants/{bot.id}/deprovision"
    assert (await client.post(url, json={"confirm": False})).status_code == 422
    response = await client.post(url, json={"confirm": True})
    assert response.status_code == 200
    assert response.json()["data"] == {"bot_id": str(bot.id), "state": "deprovisioning"}
    assert SECRET not in response.text
    flow = await db_session.scalar(select(AssistantLifecycle))
    assert flow is not None and flow.trigger == "admin" and flow.actor_id == admin.id
    audit = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "assistant.deprovision")
    )
    assert audit is not None and audit.target_id == str(bot.id)
    # 重复点不重复开流程，也不重复记审计。
    assert (await client.post(url, json={"confirm": True})).status_code == 200
    assert len((await db_session.scalars(select(AssistantLifecycle))).all()) == 1
    audits = await db_session.scalars(
        select(AuditLog).where(AuditLog.action == "assistant.deprovision")
    )
    assert len(audits.all()) == 1


async def test_confirm_must_be_the_json_true(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    await login_as(client, db_session, role="platform_admin")
    url = f"/api/admin/assistants/{bot.id}/deprovision"
    for value in ("true", 1, "yes", "1", None):
        assert (await client.post(url, json={"confirm": value})).status_code == 422, value
    assert (await client.post(url, json={})).status_code == 422
    assert await db_session.scalar(select(AssistantLifecycle)) is None
    await db_session.refresh(bot)
    assert bot.assistant_state == "active"


async def test_an_already_deprovisioned_assistant_writes_no_audit(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner, state="deprovisioned")
    bot.enabled = False
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    response = await client.post(
        f"/api/admin/assistants/{bot.id}/deprovision", json={"confirm": True}
    )
    assert response.status_code == 200 and response.json()["data"]["state"] == "deprovisioned"
    assert (
        await db_session.scalar(select(AuditLog).where(AuditLog.action == "assistant.deprovision"))
        is None
    )


async def test_an_admin_cannot_deprovision_an_ordinary_bot(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    creator = await login_as(client, db_session, role="member")
    standard = Bot(
        bot_key=f"demo-bot-{uuid.uuid4().hex[:6]}",
        platform="feishu",
        name="demo-bot",
        created_by=creator.id,
        model="m",
        working_dir="/d",
        credentials_enc="",
    )
    db_session.add(standard)
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    response = await client.post(
        f"/api/admin/assistants/{standard.id}/deprovision", json={"confirm": True}
    )
    assert response.status_code == 404
    await db_session.refresh(standard)
    assert standard.enabled and standard.assistant_state is None


async def test_toggle_and_delete_cannot_skip_the_deprovisioning(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    assert (await client.post(f"/api/admin/bots/{bot.id}/toggle")).status_code == 409
    await login_as(client, db_session, role="platform_admin")
    response = await client.delete(f"/api/admin/bots/{bot.id}")
    assert response.status_code == 409 and "停用" in response.json()["message"]
    bot.assistant_state, bot.enabled = "deprovisioned", False
    await db_session.commit()
    assert (await client.delete(f"/api/admin/bots/{bot.id}")).status_code == 200


async def test_toggle_of_ordinary_bots_is_unchanged(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    creator = await login_as(client, db_session, role="member")
    standard = Bot(
        bot_key=f"demo-bot-{uuid.uuid4().hex[:6]}",
        platform="feishu",
        name="demo-bot",
        created_by=creator.id,
        model="m",
        working_dir="/d",
        credentials_enc="",
    )
    db_session.add(standard)
    await db_session.commit()
    await login_existing(client, db_session, creator)
    response = await client.post(f"/api/admin/bots/{standard.id}/toggle")
    assert response.status_code == 200 and response.json()["data"]["enabled"] is False


async def test_delete_is_refused_while_deprovisioning(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner, state="deprovisioning")
    await login_as(client, db_session, role="platform_admin")
    response = await client.delete(f"/api/admin/bots/{bot.id}")
    assert response.status_code == 409 and "停用" in response.json()["message"]
    assert await db_session.get(Bot, bot.id, populate_existing=True) is not None


async def test_toggle_by_a_platform_admin_who_is_not_the_owner_is_not_found_404(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """别人的助理对平台管理员也不可见：启停接口按不存在回 404，记录不变。"""
    owner = await login_as(client, db_session, role="member")
    bot = await _assistant(db_session, owner)
    await login_as(client, db_session, role="platform_admin")
    response = await client.post(f"/api/admin/bots/{bot.id}/toggle")
    assert response.status_code == 404
    await db_session.refresh(bot)
    assert bot.enabled and bot.assistant_state == "active"
