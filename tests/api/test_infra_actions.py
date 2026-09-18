import time

import httpx
import jwt
from sqlalchemy import func, select

from coreman.core.auth.signatures import sign_request
from coreman.core.db.models import Bot, BusinessSystem, OutboxItem
from tests.api.conftest import login_as


def signed(path, body, secret, app_key="actions"):
    ts = str(int(time.time()))
    return {
        "X-App-Key": app_key,
        "X-Timestamp": ts,
        "X-Signature": sign_request("POST", path, body, ts, app_key, secret),
    }


async def test_push_is_durable_idempotent_and_rejects_changed_payload(client, db_session):
    user = await login_as(client, db_session, role="platform_admin")
    credential = (
        await client.post(
            "/api/admin/api-clients",
            json={"app_key": "actions", "name": "Actions", "scopes": ["push"]},
        )
    ).json()["data"]
    bot = Bot(
        bot_key="push-bot",
        name="Push",
        platform="wecom",
        created_by=user.id,
        model="test",
        working_dir="/data/test",
        credentials_enc="unused",
    )
    db_session.add(bot)
    await db_session.commit()
    body = {"bot_key": bot.bot_key, "chat_id": "user1", "content": "消息", "request_id": "test1"}
    path = "/api/infra/push"
    for _ in range(2):
        r = await client.post(path, json=body, headers=signed(path, body, credential["secret"]))
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "pending"
    assert (await db_session.scalar(select(func.count()).select_from(OutboxItem))) == 1
    changed = {**body, "content": "另一条消息"}
    assert (
        await client.post(path, json=changed, headers=signed(path, changed, credential["secret"]))
    ).status_code == 409
    assert (await client.post(path, json=body)).status_code == 401
    bot.enabled = False
    await db_session.commit()
    assert (
        await client.post(path, json=body, headers=signed(path, body, credential["secret"]))
    ).status_code == 409


async def test_system_test_requires_real_current_user_and_hides_target_secrets(
    client, db_session, monkeypatch
):
    from coreman.api.routers import infra_system_test

    user = await login_as(client, db_session, role="platform_admin", login_name="real-human")
    await client.post(
        "/api/admin/systems",
        json={"key": "example", "name": "Example", "base_url": "https://example.test/"},
    )
    credential = (
        await client.post(
            "/api/admin/api-clients",
            json={"app_key": "actions", "name": "Actions", "scopes": ["systems"]},
        )
    ).json()["data"]
    seen = []

    def handler(req):
        claims = jwt.decode(
            req.headers["cookie"].removeprefix("bot_token="), options={"verify_signature": False}
        )
        assert claims["sub"] == user.login_name and claims["exp"] - claims["iat"] == 60
        seen.append(req)
        return httpx.Response(
            200, text="synthetic-secret-body", headers={"Set-Cookie": "private=session"}
        )

    monkeypatch.setattr(
        infra_system_test,
        "make_http",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    path = "/api/admin/systems/test-access"
    body = {"system_key": "example"}
    assert (
        await client.post(path, json={**body, "email_prefix": "someone-else"})
    ).status_code == 403
    for path in ("/api/admin/systems/test-access", "/api/infra/systems/test-access"):
        r = await client.post(path, json=body, headers=signed(path, body, credential["secret"]))
        assert r.status_code == 200 and r.json()["data"]["success"], r.text
        assert "synthetic-secret" not in r.text and "private=session" not in r.text
    assert len(seen) == 2
    db_session.add(BusinessSystem(key="coreman", name="Legacy", base_url="https://example.test/"))
    await db_session.commit()
    path = "/api/admin/systems/test-access"
    assert (await client.post(path, json={"system_key": "coreman"})).status_code == 422
    assert len(seen) == 2
    client.cookies.clear()
    assert (
        await client.post(path, json=body, headers=signed(path, body, credential["secret"]))
    ).status_code in (401, 403)
    assert len(seen) == 2


async def test_system_test_uses_email_prefix_as_subject(client, db_session, monkeypatch):
    from coreman.api.routers import infra_system_test

    # 首次同步没拿到邮箱时 login_name 是平台 userid；之后补上邮箱，令牌主体应跟着用邮箱前缀。
    user = await login_as(client, db_session, role="platform_admin", login_name="ou_1a2b3c")
    user.email = "Real.Human@example.com"
    await db_session.commit()
    await client.post(
        "/api/admin/systems",
        json={"key": "example", "name": "Example", "base_url": "https://example.test/"},
    )
    subjects = []

    def handler(req):
        claims = jwt.decode(
            req.headers["cookie"].removeprefix("bot_token="), options={"verify_signature": False}
        )
        subjects.append(claims["sub"])
        return httpx.Response(302, headers={"Location": "/login"})

    monkeypatch.setattr(
        infra_system_test,
        "make_http",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    path = "/api/admin/systems/test-access"
    # 旧调用方传的 email_prefix 可以是登录名，也可以是邮箱前缀，但不能是别人。
    for prefix in ("real.human", "ou_1a2b3c"):
        r = await client.post(path, json={"system_key": "example", "email_prefix": prefix})
        assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert subjects == ["real.human", "real.human"]
    assert data["subject"] == "real.human" and data["user_login"] == "ou_1a2b3c"
    assert not data["success"] and "real.human" in data["message"]
    r = await client.post(path, json={"system_key": "example", "email_prefix": "someone-else"})
    assert r.status_code == 403
