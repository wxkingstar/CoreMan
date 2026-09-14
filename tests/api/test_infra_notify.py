import asyncio
import base64

import httpx
from sqlalchemy import func, select, update

from coreman.core.crypto import Cipher
from coreman.core.db.models import OutboxItem, PlatformApp, UserIdentity
from coreman.core.db.session import make_session_factory
from coreman.runtime.scheduler import notifications
from tests.api.conftest import MASTER_KEY, login_as
from tests.api.test_infra_actions import signed


async def setup_recipient(client, db_session):
    user = await login_as(client, db_session, role="platform_admin")
    secret = (
        await client.post(
            "/api/admin/api-clients",
            json={"app_key": "actions", "name": "Notify", "scopes": ["notify"]},
        )
    ).json()["data"]["secret"]
    cipher = Cipher(base64.b64decode(MASTER_KEY))
    app = PlatformApp(
        platform="wecom",
        name="Notify",
        capabilities=["notify"],
        corp_id="ww1",
        app_id="1000001",
        secret_enc=cipher.encrypt("synthetic-secret", "platform_apps.secret_enc"),
    )
    db_session.add_all(
        [app, UserIdentity(user_id=user.id, platform="wecom", platform_user_id="recipient")]
    )
    await db_session.commit()
    return user, secret, cipher


async def test_notification_dedupe_and_worker_retry_without_direct_api_send(
    client, db_session, db_engine, monkeypatch
):
    user, secret, cipher = await setup_recipient(client, db_session)
    body = {"wework_user_id": "recipient", "content": "提醒", "request_id": "n1"}
    for path in ("/api/robot/wework-notify", "/api/infra/notify/user"):
        r = await client.post(path, json=body, headers=signed(path, body, secret))
        assert r.status_code == 200, r.text
    item = (await db_session.execute(select(OutboxItem))).scalar_one()
    assert item.status == "pending" and item.bot_id is None and item.kind == "notify"
    factory = make_session_factory(db_engine)

    async def failure(*args):
        raise httpx.ConnectError("url includes synthetic-private-secret")

    monkeypatch.setattr(notifications, "send_notification", failure)
    assert await notifications.deliver_one(factory, cipher)
    await db_session.refresh(item)
    assert item.attempts == 1 and item.status == "pending" and item.last_error == "ConnectError"
    assert not await notifications.deliver_one(factory, cipher)
    await db_session.execute(update(OutboxItem).values(not_before=func.now()))
    await db_session.commit()
    sent = []

    async def success(session, row, cipher):
        sent.append(row.id)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(notifications, "send_notification", success)
    results = await asyncio.gather(
        notifications.deliver_one(factory, cipher), notifications.deliver_one(factory, cipher)
    )
    assert sorted(results) == [False, True] and sent == [item.id]
    await db_session.refresh(item)
    assert item.status == "sent"


async def test_notification_rechecks_disabled_recipient_and_rejects_broadcast(
    client, db_session, db_engine
):
    user, secret, cipher = await setup_recipient(client, db_session)
    path = "/api/infra/notify/user"
    body = {"user_login": user.login_name, "content": "提醒"}
    assert (
        await client.post(path, json=body, headers=signed(path, body, secret))
    ).status_code == 200
    user.status = "disabled"
    await db_session.commit()
    assert await notifications.deliver_one(make_session_factory(db_engine), cipher)
    item = (await db_session.execute(select(OutboxItem))).scalar_one()
    assert item.status == "pending" and item.attempts == 1 and "停用" in item.last_error
    for recipient in ("@all", "user1|user2"):
        body = {"platform_user_id": recipient, "content": "提醒"}
        assert (
            await client.post(path, json=body, headers=signed(path, body, secret))
        ).status_code == 422
