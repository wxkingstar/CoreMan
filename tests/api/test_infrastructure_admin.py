import base64

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.tokens import active_key, issue_token
from coreman.core.crypto import Cipher
from coreman.core.db.models import ApiClient, AuditLog, BotSystemGrant
from tests.api.conftest import MASTER_KEY, login_as, login_existing
from tests.api.test_bots import _bot_body


async def test_clients_secret_only_once_and_rotation_invalidates_previous(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="platform_admin")
    r = await client.post(
        "/api/admin/api-clients", json={"app_key": "test-client", "name": "Test", "scopes": ["org"]}
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    secret = data["secret"]
    assert secret not in (await client.get("/api/admin/api-clients")).text
    cipher = Cipher(base64.b64decode(MASTER_KEY))
    row = await db_session.get(ApiClient, "test-client")
    assert row is not None and cipher.decrypt(row.secret_enc, "api_clients.secret_enc") == secret
    r = await client.post(
        "/api/admin/api-clients/test-client/rotate-secret",
        headers={"If-Match": str(data["version"])},
    )
    assert r.status_code == 200 and r.json()["data"]["secret"] != secret
    await db_session.refresh(row)
    assert cipher.decrypt(row.secret_enc, "api_clients.secret_enc") != secret
    logs = list((await db_session.execute(select(AuditLog))).scalars())
    assert secret not in str([x.diff for x in logs])
    await login_as(client, db_session, role="ai_committee")
    assert (await client.get("/api/admin/api-clients")).status_code == 403
    assert (await client.post("/api/admin/jwt-keys/rotate")).status_code == 403


async def test_system_grants_whitelist_reclaim_and_optimistic_lock(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="team_lead")
    bot = (await client.post("/api/admin/bots", json=_bot_body())).json()["data"]
    manager = await login_as(client, db_session, role="ai_committee")
    r = await client.post("/api/admin/systems", json={"key": "erp", "name": "ERP"})
    assert r.status_code == 201, r.text
    system = r.json()["data"]
    await login_existing(client, db_session, owner)
    r = await client.put(
        f"/api/admin/bots/{bot['id']}/system-grants",
        json={"system_keys": ["erp"]},
        headers={"If-Match": str(bot["version"])},
    )
    assert r.status_code == 200, r.text
    assert (
        await client.put(
            f"/api/admin/bots/{bot['id']}/system-grants",
            json={"system_keys": []},
            headers={"If-Match": str(bot["version"])},
        )
    ).status_code == 409
    await login_existing(client, db_session, manager)
    assert (await client.get(f"/api/admin/bots/{bot['id']}/system-grants")).status_code == 403
    r = await client.put(
        "/api/admin/systems/erp",
        json={"name": "ERP", "allowed_bot_ids": []},
        headers={"If-Match": str(system["version"])},
    )
    assert r.status_code == 200, r.text
    assert not list((await db_session.execute(select(BotSystemGrant))).scalars())
    await login_existing(client, db_session, owner)
    version = (await client.get(f"/api/admin/bots/{bot['id']}/system-grants")).json()["data"][
        "version"
    ]
    r = await client.put(
        f"/api/admin/bots/{bot['id']}/system-grants",
        json={"system_keys": ["erp"]},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 403


async def test_bot_token_role_csrf_and_disabled_user(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await login_as(client, db_session, role="platform_admin")
    cipher = Cipher(base64.b64decode(MASTER_KEY))
    key = await active_key(db_session, cipher)
    token = issue_token(
        key,
        cipher,
        issuer="coreman",
        login=user.login_name or "",
        name=user.display_name,
        audience="coreman",
        ttl=3600,
    )
    wrong_scope = issue_token(
        key,
        cipher,
        issuer="coreman",
        login=user.login_name or "",
        name=user.display_name,
        audience="erp",
        ttl=3600,
    )
    await db_session.commit()
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    client.cookies.set("bot_token", token)
    assert (await client.get("/api/admin/auth/me")).status_code == 200
    assert (
        await client.post("/api/admin/systems", json={"key": "example", "name": "Example"})
    ).status_code == 201
    client.cookies.set("bot_token", wrong_scope)
    assert (
        await client.post("/api/admin/systems", json={"key": "evil", "name": "Evil"})
    ).status_code == 401
    client.cookies.set("bot_token", token)
    user.status = "disabled"
    await db_session.commit()
    assert (await client.get("/api/admin/auth/me")).status_code == 401
    assert (
        await client.post("/api/admin/systems", json={"key": "evil", "name": "Evil"})
    ).status_code == 401
    # JWKS 不需要登录，且只含公钥。
    r = await client.get("/api/.well-known/jwks.json")
    assert r.status_code == 200 and "d" not in r.json()["keys"][0]
