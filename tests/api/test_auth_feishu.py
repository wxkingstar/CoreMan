import base64
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from coreman.api.security import SESSION_COOKIE
from coreman.core.crypto import Cipher
from coreman.core.db.models import AdminSession, AuthNonce, PlatformApp, User, UserIdentity
from coreman.core.platforms.feishu import FeishuClient
from tests.api.conftest import MASTER_KEY


async def seed(session):
    app = PlatformApp(
        platform="feishu",
        name="飞书登录",
        capabilities=["login"],
        app_id="cli_test",
        secret_enc=Cipher(base64.b64decode(MASTER_KEY)).encrypt(
            "synthetic-secret", "platform_apps.secret_enc"
        ),
    )
    user = User(login_name="employee1", display_name="员工", source="sync")
    session.add_all([app, user])
    await session.flush()
    session.add(UserIdentity(user_id=user.id, platform="feishu", platform_user_id="employee1"))
    await session.commit()
    return app, user


async def start(client):
    response = await client.get("/api/auth/feishu/start", params={"redirect": "/bots"})
    url = urlparse(response.headers["location"])
    assert url.netloc == "open.feishu.cn"
    return parse_qs(url.query)["state"][0]


async def test_feishu_login_bound_single_use_and_no_user_creation(client, db_session, monkeypatch):
    app, user = await seed(db_session)
    assert (await client.get("/api/auth/providers")).json()["data"]["feishu"]
    calls = []

    async def info(self, code):
        calls.append(code)
        return {"user_id": "employee1", "open_id": "ou"}

    monkeypatch.setattr(FeishuClient, "user_info_by_code", info)
    state = await start(client)
    response = await client.get(
        "/api/auth/feishu/callback", params={"code": "synthetic-code", "state": state}
    )
    assert response.headers["location"] == "/bots"
    assert SESSION_COOKIE in response.headers["set-cookie"]
    sessions = list((await db_session.scalars(select(AdminSession))).all())
    assert len(sessions) == 1 and sessions[0].user_id == user.id
    assert sessions[0].auth_method == "feishu_oauth"
    response = await client.get(
        "/api/auth/feishu/callback", params={"code": "synthetic-code", "state": state}
    )
    assert response.headers["location"].endswith("invalid_state")
    assert len(calls) == 1
    assert not (
        await db_session.scalars(select(AuthNonce).where(AuthNonce.value == "synthetic-code"))
    ).all()


async def test_feishu_login_rejects_other_browser_and_changed_app(client, db_session, monkeypatch):
    app, _ = await seed(db_session)

    async def no_call(*args, **kwargs):
        raise AssertionError("platform call must not occur")

    monkeypatch.setattr(FeishuClient, "user_info_by_code", no_call)
    state = await start(client)
    client.cookies.clear()
    response = await client.get("/api/auth/feishu/callback", params={"code": "c", "state": state})
    assert response.headers["location"].endswith("invalid_state")
    state = await start(client)
    app.name = "changed"
    await db_session.commit()
    response = await client.get("/api/auth/feishu/callback", params={"code": "c", "state": state})
    assert response.headers["location"].endswith("invalid_state")
