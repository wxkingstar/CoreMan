import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape

import httpx
import pytest
from sqlalchemy import select, update

from coreman.core.auth.signatures import sign_request
from coreman.core.db.models import (
    ApiClient,
    Escalation,
    OutboxItem,
    PlatformApp,
    User,
    UserIdentity,
)
from coreman.core.db.session import make_session_factory
from coreman.core.escalations import service
from coreman.core.notifications import NotificationSkipped, escalation_current
from tests.api.conftest import login_existing
from tests.integration.worker_helpers import seed_bot
from tests.unit.test_callback_crypto import AES_KEY, encrypted_message


class Signed(httpx.Auth):
    """按实际路径、查询参数和 JSON 正文给每个请求签名；回调等其它路由忽略这些头。"""

    def __init__(self, app_key: str, secret: str) -> None:
        self.app_key, self.secret = app_key, secret

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        params = dict(request.url.params)
        if request.content and "application/json" in request.headers.get("content-type", ""):
            params.update(json.loads(request.content))
        ts = str(int(time.time()))
        request.headers["X-App-Key"] = self.app_key
        request.headers["X-Timestamp"] = ts
        request.headers["X-Signature"] = sign_request(
            request.method, request.url.path, params, ts, self.app_key, self.secret
        )
        yield request


async def setup(client, session):
    bot, _, cipher = await seed_bot(session)
    creator = await session.get(User, bot.created_by)
    recipient = User(login_name="recipient", display_name="接收人")
    session.add(recipient)
    await session.flush()
    session.add_all(
        [
            UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="creator"),
            UserIdentity(user_id=recipient.id, platform="wecom", platform_user_id="recipient"),
            ApiClient(
                app_key="esc",
                name="Caller",
                scopes=["escalations"],
                secret_enc=cipher.encrypt("client-secret", "api_clients.secret_enc"),
            ),
        ]
    )
    app = PlatformApp(
        platform="wecom",
        name="Notify",
        capabilities=["notify", "callback"],
        corp_id="ww-test",
        app_id="1000001",
        secret_enc=cipher.encrypt("unused", "platform_apps.secret_enc"),
        callback_token_enc=cipher.encrypt("token", "platform_apps.callback_token_enc"),
        callback_aes_key_enc=cipher.encrypt(AES_KEY, "platform_apps.callback_aes_key_enc"),
    )
    session.add(app)
    await session.commit()
    await login_existing(client, session, creator)
    client.auth = Signed("esc", "client-secret")
    return bot, recipient, app, cipher


def callback_body(app, msg_id="1", content="收到", *, agent_id=None):
    now = str(int(datetime.now(UTC).timestamp()))
    plain = (
        f"<xml><ToUserName>ww-test</ToUserName><AgentID>{agent_id or app.app_id}</AgentID>"
        f"<FromUserName>recipient</FromUserName><MsgType>text</MsgType>"
        f"<Content>{escape(content)}</Content><MsgId>{msg_id}</MsgId>"
        f"<CreateTime>{now}</CreateTime></xml>"
    )
    encrypted, _ = encrypted_message(plain.encode())
    signature = hashlib.sha1(
        "".join(sorted(("token", now, "nonce", encrypted))).encode()
    ).hexdigest()
    return {
        "msg_signature": signature,
        "timestamp": now,
        "nonce": "nonce",
    }, f"<xml><Encrypt>{encrypted}</Encrypt></xml>"


@pytest.mark.parametrize("legacy", [False, True])
async def test_escalation_fifo_callback_dedupe_and_followups(client, db_session, legacy):
    bot, recipient, app, _ = await setup(client, db_session)
    body = {
        "bot_key": bot.bot_key,
        "to_user_id": "recipient",
        "from_user_id": "creator",
        "question": "请确认结果",
        "request_id": "first",
    }
    path = "/api/escalation" if legacy else "/api/infra/escalations"
    create_path = path + "/create" if legacy else path
    poll_suffix = "/poll" if legacy else ""
    result = await client.post(create_path, json=body)
    assert result.status_code == 200, result.text
    first = result.json()["data"]
    assert first["from_user_id"] == "creator" and first["to_user_id"] == "recipient"
    repeat = await client.post("/api/escalation/create", json=body)
    assert repeat.json()["data"]["escalation_id"] == first["escalation_id"]
    assert (
        await client.post(create_path, json=body | {"question": "different"})
    ).status_code == 409
    second = (await client.post(create_path, json=body | {"request_id": "second"})).json()["data"]
    assert second["status"] == "queued"
    await db_session.execute(update(OutboxItem).values(status="sent"))
    await db_session.commit()
    query, raw = callback_body(app)
    callback = f"/api/callbacks/wecom/{app.id}"
    for _ in range(2):
        response = await client.post(callback, params=query, content=raw)
        assert response.status_code == 200 and response.text == "success", response.text
    result = await client.get(f"{path}/{first['group_id']}{poll_suffix}")
    assert result.json()["data"]["status"] == "replied"
    assert len(result.json()["data"]["replies"]) == 1
    follow = f"{path}/{first['escalation_id']}/followup"
    assert (await client.post(follow, json={"question": "补充一下"})).status_code == 200
    assert (await client.post(follow, json={"question": "重复催办"})).status_code == 409
    await db_session.execute(update(OutboxItem).values(status="sent"))
    await db_session.commit()
    query, raw = callback_body(app, msg_id="2", content="已处理")
    assert (await client.post(callback, params=query, content=raw)).status_code == 200
    result = await client.get(f"{path}/{first['group_id']}{poll_suffix}")
    assert result.json()["data"]["resolution"] == "offline"
    result = await client.get(f"{path}/{second['group_id']}{poll_suffix}")
    assert result.json()["data"]["status"] == "pending"


@pytest.mark.parametrize("path", ["/api/escalation/create", "/api/infra/escalations"])
async def test_unsigned_secret_rejected_and_sender_cannot_be_forged(client, db_session, path):
    bot, _, _, _ = await setup(client, db_session)
    body = {
        "bot_key": bot.bot_key,
        "to_user_id": "recipient",
        "question": "确认",
        "from_user_id": "recipient",
    }
    assert (await client.post(path, json=body)).status_code == 403
    # 旧的 X-App-Key + X-API-Key 明文 secret 鉴权已移除：不签名一律 401。
    client.auth = None
    legacy = {"X-App-Key": "esc", "X-API-Key": "client-secret"}
    assert (await client.post(path, json=body, headers=legacy)).status_code == 401
    assert await db_session.scalar(select(Escalation)) is None
    client.auth = Signed("esc", "client-secret")
    client.cookies.clear()
    assert (await client.post(path, json=body)).status_code == 403
    body.pop("from_user_id")
    result = await client.post(path, json=body)
    assert result.status_code == 200 and result.json()["data"]["from_user_id"] is None


async def test_callback_app_binding_and_terminal_notification_check(client, db_session):
    import pytest

    bot, _, app, _ = await setup(client, db_session)
    data = (
        await client.post(
            "/api/infra/escalations",
            json={"bot_key": bot.bot_key, "to_user_id": "recipient", "question": "确认"},
        )
    ).json()["data"]
    query, raw = callback_body(app, agent_id="wrong-app")
    assert (
        await client.post(f"/api/callbacks/wecom/{app.id}", params=query, content=raw)
    ).status_code == 403
    await client.post(f"/api/infra/escalations/{data['group_id']}/cancel")
    item = await db_session.scalar(select(OutboxItem))
    with pytest.raises(NotificationSkipped):
        await escalation_current(db_session, item)


async def test_timer_nudges_abandon_and_no_revival(client, db_session, db_engine):
    bot, _, _, _ = await setup(client, db_session)
    data = (
        await client.post(
            "/api/infra/escalations",
            json={"bot_key": bot.bot_key, "to_user_id": "recipient", "question": "确认"},
        )
    ).json()["data"]
    row = await db_session.scalar(select(Escalation))
    now = datetime.now(UTC)
    row.activated_at = now - timedelta(seconds=481)
    row.last_polled_at = now
    await db_session.commit()
    factory = make_session_factory(db_engine)

    async def tick():
        async with factory() as session:
            result = await service.tick(session, now)
            await session.commit()
            return result

    counts = await asyncio.gather(tick(), tick())
    assert sum(counts) == 1
    await db_session.refresh(row)
    assert row.nudge_stage == 1
    row.last_polled_at = now - timedelta(seconds=301)
    await db_session.commit()
    result = await client.get(f"/api/infra/escalations/{data['group_id']}")
    assert result.json()["data"]["status"] == "expired"


@pytest.mark.parametrize("mode", ["ready", "failed", "cancelled", "reaped"])
async def test_media_download_signed_link_and_expiry(
    client, db_session, db_engine, monkeypatch, tmp_path, mode
):
    from coreman.core.config import reset_settings_cache
    from coreman.runtime.worker import escalation_media
    from coreman.runtime.worker.escalation_media import EscalationMediaHandler
    from tests.integration.test_cron_handler import claim
    from tests.integration.worker_helpers import build_ctx

    bot, _, app, _ = await setup(client, db_session)
    data = (
        await client.post(
            "/api/infra/escalations",
            json={"bot_key": bot.bot_key, "to_user_id": "recipient", "question": "请给附件"},
        )
    ).json()["data"]
    await db_session.execute(update(OutboxItem).values(status="sent"))
    await db_session.commit()
    now = str(int(datetime.now(UTC).timestamp()))
    plain = (
        f"<xml><ToUserName>ww-test</ToUserName><AgentID>{app.app_id}</AgentID>"
        f"<FromUserName>recipient</FromUserName><MsgType>file</MsgType><MediaId>media-test</MediaId>"
        f"<MsgId>media-1</MsgId><CreateTime>{now}</CreateTime></xml>"
    )
    encrypted, _ = encrypted_message(plain.encode())
    signature = hashlib.sha1(
        "".join(sorted(("token", now, "nonce", encrypted))).encode()
    ).hexdigest()
    response = await client.post(
        f"/api/callbacks/wecom/{app.id}",
        params={"msg_signature": signature, "timestamp": now, "nonce": "nonce"},
        content=f"<xml><Encrypt>{encrypted}</Encrypt></xml>",
    )
    assert response.status_code == 200, response.text

    class MediaClient:
        def __init__(self, *args):
            pass

        async def media_stream(self, media_id):
            assert media_id == "media-test"
            if mode == "failed":
                raise RuntimeError("mock download failed")
            yield b"safe "
            yield b"attachment"

        async def aclose(self):
            pass

    monkeypatch.setattr(escalation_media, "WeComClient", MediaClient)
    monkeypatch.setenv("OBJECT_STORAGE_ROOT", str(tmp_path))
    reset_settings_cache()
    # API 的 settings 与 worker 的 settings 使用同一测试目录。
    from coreman.core.config import get_settings

    client_app_settings = get_settings()
    from coreman.api.routers import objects

    real_store = objects.LocalObjectStore
    monkeypatch.setattr(
        objects, "LocalObjectStore", lambda _root, key, base: real_store(str(tmp_path), key, base)
    )
    task = await claim(db_session)
    from coreman.core.bus import tasks
    from coreman.core.escalations.media_recovery import recover

    if mode == "cancelled":
        await tasks.request_cancel(db_session, task.id, "test cancel")
        await db_session.commit()
    if mode == "reaped":
        await tasks.finish(db_session, task.id, status="failed", error_code="worker_lost")
        await db_session.commit()
        assert await recover(db_session, datetime.now(UTC)) == 1
        await db_session.commit()
        assert await recover(db_session, datetime.now(UTC)) == 0
    else:
        await EscalationMediaHandler().run(build_ctx(db_engine, task))
    result = (await client.get(f"/api/infra/escalations/{data['group_id']}")).json()["data"]
    reply = result["replies"][0]
    if mode != "ready":
        assert reply["media"]["status"] == "failed", reply
        assert "url" not in reply["media"]
        from coreman.core.db.models import ChatLog, UserReached

        assert await db_session.scalar(select(ChatLog)) is None
        assert await db_session.scalar(select(UserReached)) is None
        return
    assert reply["media"]["status"] == "ready", reply
    url = reply["media"]["url"]
    response = await client.get(url)
    assert response.status_code == 200 and response.content == b"safe attachment"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (await client.get(url.replace("signature=", "signature=x"))).status_code == 403
    assert client_app_settings.object_storage_root == str(tmp_path)


async def test_group_winner_client_isolation_and_followup_delivery(client, db_session):
    bot, _, app, cipher = await setup(client, db_session)
    extra = User(id=uuid.UUID(int=1), login_name="other", display_name="Other")
    db_session.add(extra)
    await db_session.flush()
    db_session.add_all(
        [
            UserIdentity(user_id=extra.id, platform="wecom", platform_user_id="other"),
            ApiClient(
                app_key="other-client",
                name="Other",
                scopes=["escalations"],
                secret_enc=cipher.encrypt("other-secret", "api_clients.secret_enc"),
            ),
        ]
    )
    await db_session.commit()
    base = "/api/infra/escalations"
    response = await client.post(
        base,
        json={
            "bot_key": bot.bot_key,
            "targets": [{"to_user_id": "recipient"}, {"to_user_id": "other"}],
            "question": "Who knows?",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    path = f"{base}/{data['group_id']}"
    client.auth = Signed("other-client", "other-secret")
    assert (await client.get(path)).status_code == 404
    assert (await client.post(f"{path}/cancel")).status_code == 404
    client.auth = Signed("esc", "client-secret")
    await db_session.execute(update(OutboxItem).values(status="sent"))
    await db_session.commit()
    query, raw = callback_body(app, msg_id="group-first")
    callback = f"/api/callbacks/wecom/{app.id}"
    assert (await client.post(callback, params=query, content=raw)).status_code == 200
    data = (await client.get(path)).json()["data"]
    assert data["to_user_id"] == "recipient"
    assert sorted(row["status"] for row in data["escalations"]) == ["cancelled", "replied"]
    for round_no in (1, 2):
        assert (
            await client.post(f"{path}/followup", json={"question": f"round {round_no}"})
        ).status_code == 200
        query, raw = callback_body(app, msg_id=f"before-delivery-{round_no}")
        await client.post(callback, params=query, content=raw)
        data = (await client.get(path)).json()["data"]
        assert len(data["replies"]) == round_no
        await db_session.execute(update(OutboxItem).values(status="sent"))
        await db_session.commit()
        query, raw = callback_body(app, msg_id=f"round-{round_no}")
        await client.post(callback, params=query, content=raw)
    assert (await client.post(f"{path}/followup", json={"question": "third"})).status_code == 409
    assert (await client.post(f"{path}/resolve", json={"resolution": "agent"})).status_code == 200
    data = (await client.get(path)).json()["data"]
    assert data["resolution"] == "agent" and data["followup_questions"] == ["round 1", "round 2"]


async def test_failed_question_delivery_ends_escalation_with_reason(client, db_session, db_engine):
    from coreman.core.bus import outbox

    bot, _, _, _ = await setup(client, db_session)
    base = "/api/infra/escalations"

    async def create(question):
        body = {"bot_key": bot.bot_key, "to_user_id": "recipient", "question": question}
        response = await client.post(base, json=body)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    first, second = await create("Q1"), await create("Q2")
    assert first["status"] == "pending" and second["status"] == "queued"
    assert first["delivery_failed"] is False and first["failure_reason"] is None
    ask = await db_session.scalar(
        select(OutboxItem).where(
            OutboxItem.dedupe_key == f"escalation:{first['escalation_id']}:ask:0:0"
        )
    )
    # 重试耗尽 / 平台永久拒绝：通知判死之后，求助不能继续挂成 pending 让 Agent 空等。
    await outbox.fail(db_session, ask.id, "WeComError (60020)")
    await db_session.commit()
    async with make_session_factory(db_engine)() as session:
        assert await service.tick(session, datetime.now(UTC)) == 1
        await session.commit()
    data = (await client.get(f"{base}/{first['escalation_id']}")).json()["data"]
    assert data["status"] == "cancelled" and data["delivery_failed"] is True
    assert data["failure_reason"] == "WeComError (60020)"
    # 失败的那条腾出位置，排队的下一条照常激活；Agent 自己取消的不算发送失败。
    data = (await client.get(f"{base}/{second['escalation_id']}")).json()["data"]
    assert data["status"] == "pending" and data["delivery_failed"] is False
    assert (await client.post(f"{base}/{second['escalation_id']}/cancel")).status_code == 200
    data = (await client.get(f"{base}/{second['escalation_id']}")).json()["data"]
    assert data["status"] == "cancelled" and data["delivery_failed"] is False


async def test_unreachable_recipient_is_cancelled_instead_of_left_pending(client, db_session):
    bot, recipient, _, _ = await setup(client, db_session)
    base = "/api/infra/escalations"
    body = {"bot_key": bot.bot_key, "to_user_id": "recipient", "question": "Q1"}
    first = (await client.post(base, json=body)).json()["data"]
    second = (await client.post(base, json={**body, "question": "Q2"})).json()["data"]
    assert second["status"] == "queued"
    ident = await db_session.scalar(
        select(UserIdentity).where(UserIdentity.user_id == recipient.id)
    )
    ident.platform_user_id = "recipient-rebound"
    await db_session.commit()
    # 排队的那条轮到激活时，接收人账号已变更：通知入不了队，直接结束而不是挂成 pending。
    assert (await client.post(f"{base}/{first['escalation_id']}/cancel")).status_code == 200
    data = (await client.get(f"{base}/{second['escalation_id']}")).json()["data"]
    assert data["status"] == "cancelled" and data["delivery_failed"] is True
    assert data["failure_reason"] == service.NOTIFICATION_UNAVAILABLE


async def test_history_prevents_destructive_config_delete(client, db_session):
    bot, _, app, _ = await setup(client, db_session)
    actor = await db_session.get(User, bot.created_by)
    actor.role = "platform_admin"
    await db_session.commit()
    result = await client.post(
        "/api/infra/escalations",
        json={"bot_key": bot.bot_key, "to_user_id": "recipient", "question": "retain history"},
    )
    assert result.status_code == 200
    assert (await client.delete(f"/api/admin/platform-apps/{app.id}")).status_code == 409
    assert (await client.delete(f"/api/admin/bots/{bot.id}")).status_code == 409
    assert (
        await client.get(f"/api/infra/escalations/{result.json()['data']['group_id']}")
    ).status_code == 200


async def test_escalation_notification_uses_recipient_japanese(client, db_session):
    bot, recipient, _, _ = await setup(client, db_session)
    recipient.locale = "ja"
    await db_session.commit()
    result = await client.post(
        "/api/infra/escalations",
        json={
            "bot_key": bot.bot_key,
            "target_user_ids": [str(recipient.id)],
            "question": "確認をお願いします",
        },
    )
    assert result.status_code == 200, result.text
    item = await db_session.scalar(select(OutboxItem))
    assert "確認依頼" in item.payload["content"] and "対応済み" in item.payload["content"]
    assert "已处理" not in item.payload["content"]
