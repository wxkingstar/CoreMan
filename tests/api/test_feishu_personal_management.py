from datetime import UTC, datetime, timedelta

from coreman.core.db.models import FeishuPersonalGrant, User
from tests.api.conftest import login_existing
from tests.api.test_feishu_personal import grant, setup

URL = "/api/me/feishu-authorizations"


async def test_grants_are_own_only_and_revoke_requires_csrf(client, app, db_session, monkeypatch):
    from unittest.mock import AsyncMock

    from coreman.core.feishu_personal import revocation

    monkeypatch.setattr(revocation, "revoke_tokens", AsyncMock(return_value=True))
    bot, owner, _ = await setup(db_session, app)
    own = await grant(db_session, app, bot, owner)
    other = User(login_name="other-personal-user", display_name="Other", role="platform_admin")
    db_session.add(other)
    await db_session.flush()
    foreign = FeishuPersonalGrant(
        bot_id=bot.id,
        user_id=other.id,
        app_id="cli_test",
        platform_user_id="other",
        open_id="ou_other",
        tenant_key="tenant-test",
        status="pending",
        pending_enc="private-pending-secret",
        pending_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(foreign)
    await db_session.commit()
    await login_existing(client, db_session, other)
    response = await client.get(URL)
    items = response.json()["data"]["items"]
    assert len(items) == 1 and items[0]["status"] == "expired"
    assert items[0]["expires_at"] is not None
    assert "private-pending-secret" not in response.text and "token_enc" not in response.text
    assert response.headers["cache-control"] == "no-store"
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await client.delete(f"{URL}/{bot.id}")).status_code == 403
    await db_session.refresh(foreign)
    assert foreign.pending_enc is not None
    client.headers["X-CSRF-Token"] = csrf
    assert (await client.delete(f"{URL}/{bot.id}")).json()["data"] == {
        "ok": True,
        "remote_revoked": False,
    }
    await db_session.refresh(foreign)
    await db_session.refresh(own)
    assert foreign.status == "revoked" and foreign.pending_enc is None
    assert own.status == "connected" and own.token_enc is not None
    await login_existing(client, db_session, owner)
    assert (await client.get(URL)).json()["data"]["items"][0]["status"] == "connected"
    assert (await client.delete(f"{URL}/{bot.id}")).status_code == 200
    await db_session.refresh(own)
    assert own.token_enc is None and own.pending_enc is None


async def test_bot_token_cannot_list_or_revoke_its_owners_grant(
    client, app, db_session, monkeypatch
):
    from coreman.api import bot_auth

    bot, owner, _ = await setup(db_session, app)
    row = await grant(db_session, app, bot, owner)
    await login_existing(client, db_session, owner)

    async def token_user(request, session):
        request.state.bot_token_authenticated = True
        request.state.admin_session = None
        return owner

    monkeypatch.setattr(bot_auth, "token_user", token_user)
    client.cookies.set("bot_token", "valid-human-bot-token")
    assert (await client.get(URL)).status_code == 403
    assert (await client.delete(f"{URL}/{bot.id}")).status_code == 403
    await db_session.refresh(row)
    assert row.token_enc is not None


async def test_mcp_rejects_browser_and_tool_notifications(client, app, db_session):
    from tests.api.test_feishu_personal import URL as MCP_URL
    from tests.api.test_feishu_personal import headers

    _, owner, task = await setup(db_session, app)
    auth = await headers(app, task, owner)
    response = await client.post(
        MCP_URL, headers={**auth, "Origin": "https://evil.example"}, json={}
    )
    assert response.status_code == 403
    for body in (
        '{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}',
        '{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
        '{"jsonrpc":"2.0","id":true,"method":"ping"}',
        '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"feishu_authorize"}}',
        '{"jsonrpc":"2.0","id":1,"method":"tools/list","actor":"foreign"}',
    ):
        response = await client.post(MCP_URL, headers=auth, content=body)
        assert "error" in response.json()
