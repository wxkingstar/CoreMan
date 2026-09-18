import base64
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from structlog.testing import capture_logs

from coreman.api.security import SESSION_COOKIE
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    AdminSession,
    AuditLog,
    AuthNonce,
    PlatformApp,
    User,
    UserIdentity,
)
from coreman.core.platforms.feishu import FeishuClient, FeishuError
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


def directory_row(user_id="newbie", **kw):
    return {
        "user_id": user_id,
        "open_id": "ou_new",
        "union_id": "on_new",
        "name": "新同事",
        "enterprise_email": "newbie@example.com",
        "department_ids": [],
        **kw,
    }


async def scan_as(client, monkeypatch, user_id, directory):
    """以 user_id 扫码；directory(path) 模拟飞书通讯录接口，返回 body 或抛 FeishuError。"""
    paths = []

    async def info(self, code):
        return {"user_id": user_id}

    async def call(self, method, path, **kwargs):
        paths.append(path)
        return directory(path)

    monkeypatch.setattr(FeishuClient, "user_info_by_code", info)
    monkeypatch.setattr(FeishuClient, "call", call)
    state = await start(client)
    with capture_logs() as logs:
        response = await client.get(
            "/api/auth/feishu/callback", params={"code": f"code-{user_id}", "state": state}
        )
    return response, paths, logs


async def test_unknown_user_is_synced_from_directory_then_logged_in(
    client, db_session, monkeypatch
):
    app, _ = await seed(db_session)
    app.capabilities = ["login", "contact_sync"]
    await db_session.commit()
    response, paths, _ = await scan_as(
        client, monkeypatch, "newbie", lambda path: {"data": {"user": directory_row()}}
    )
    assert paths == ["/open-apis/contact/v3/users/newbie"]
    assert response.headers["location"] == "/bots"
    user = await db_session.scalar(select(User).where(User.login_name == "newbie"))
    assert user.display_name == "新同事" and user.source == "sync" and user.status == "active"
    ident = await db_session.scalar(
        select(UserIdentity).where(UserIdentity.platform_user_id == "newbie")
    )
    assert ident.user_id == user.id and ident.open_id == "ou_new"
    session_row = await db_session.scalar(select(AdminSession))
    assert session_row.user_id == user.id
    actions = list(await db_session.scalars(select(AuditLog.action).order_by(AuditLog.id)))
    assert actions == ["user.login_sync", "auth.feishu_login"]


async def test_user_outside_directory_scope_stays_unknown_and_logs_user_id(
    client, db_session, monkeypatch
):
    app, _ = await seed(db_session)
    app.capabilities = ["login", "contact_sync"]
    await db_session.commit()

    def outside(path):
        raise FeishuError(41050, "no user authority")

    response, paths, logs = await scan_as(client, monkeypatch, "outsider", outside)
    assert response.headers["location"].endswith("user_not_found")
    assert paths == ["/open-apis/contact/v3/users/outsider"]
    assert [u.login_name for u in await db_session.scalars(select(User))] == ["employee1"]
    events = {e["event"]: e for e in logs}
    assert events["feishu_login_sync_failed"]["errcode"] == 41050
    assert events["feishu_login_unknown_user"]["user_id"] == "outsider"


async def test_resigned_member_is_not_created(client, db_session, monkeypatch):
    app, _ = await seed(db_session)
    app.capabilities = ["login", "contact_sync"]
    await db_session.commit()
    row = directory_row(status={"is_resigned": True})
    response, _, _ = await scan_as(
        client, monkeypatch, "newbie", lambda path: {"data": {"user": row}}
    )
    assert response.headers["location"].endswith("user_not_found")
    assert not await db_session.scalar(select(User).where(User.login_name == "newbie"))


async def test_login_only_app_never_reads_directory(client, db_session, monkeypatch):
    await seed(db_session)

    def forbidden(path):
        raise AssertionError("directory must not be read without contact_sync")

    response, paths, logs = await scan_as(client, monkeypatch, "stranger", forbidden)
    assert response.headers["location"].endswith("user_not_found")
    assert paths == []
    assert any(
        e["event"] == "feishu_login_unknown_user" and e["user_id"] == "stranger" for e in logs
    )


async def test_separate_contact_sync_app_is_used_for_lookup(client, db_session, monkeypatch):
    await seed(db_session)
    db_session.add(
        PlatformApp(
            platform="feishu",
            name="飞书通讯录",
            capabilities=["contact_sync"],
            app_id="cli_contacts",
            secret_enc=Cipher(base64.b64decode(MASTER_KEY)).encrypt(
                "synthetic-secret", "platform_apps.secret_enc"
            ),
        )
    )
    await db_session.commit()
    used = []
    original = FeishuClient.__init__

    def track(self, app_id, secret, **kwargs):
        used.append(app_id)
        original(self, app_id, secret, **kwargs)

    monkeypatch.setattr(FeishuClient, "__init__", track)
    response, _, _ = await scan_as(
        client, monkeypatch, "newbie", lambda path: {"data": {"user": directory_row()}}
    )
    assert response.headers["location"] == "/bots"
    assert used == ["cli_test", "cli_contacts"]
