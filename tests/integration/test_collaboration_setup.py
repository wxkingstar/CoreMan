from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.chat import collaboration_setup as service
from coreman.core.crypto import Cipher
from coreman.core.db.models import OutboxItem, RelayServer, RuntimeNode, Task, User
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.gateway_feishu.transport import FeishuTransport
from tests.integration.test_bot_collaboration import setup
from tests.integration.worker_helpers import MASTER


class Client:
    def __init__(self, app_id, secret):
        self.app_id = app_id

    async def aclose(self):
        pass

    async def call(self, method, path, **kwargs):
        if path.endswith("/info/"):
            return {"bot": {"open_id": "open-" + self.app_id}}
        assert path.endswith("/chats")
        return {"data": {"has_more": False, "items": [{"chat_id": "group", "name": "测试群"}]}}


async def prepared(session, monkeypatch):
    a, b, _, route, _, _ = await setup(session)
    cipher = Cipher(MASTER)
    for bot, app in [(a, "app-a"), (b, "app-b")]:
        bot.credentials_enc = encrypt_json(
            cipher, {"app_id": app, "app_secret": "test"}, CREDENTIALS_AAD
        )
    relay = await session.get(RelayServer, a.relay_server_id)
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    node.heartbeat_at = datetime.now(UTC)
    owner = await session.get(User, a.created_by)
    await session.commit()
    monkeypatch.setattr(service, "FeishuClient", Client)
    await service.begin(session, route, a, b, owner, cipher)
    await session.commit()
    return a, b, route, owner, cipher


async def receipt(session, receiver, item, app, own_open, sender_union, tenant="tenant"):
    mid = "probe-" + str(item.id)
    item.status = "sent"
    item.payload = {**item.payload, "_feishu_message_id": mid}
    raw = {
        "header": {"app_id": app, "tenant_key": tenant, "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "bot", "sender_id": {"union_id": sender_union}},
            "message": {
                "message_id": mid,
                "chat_id": "group",
                "chat_type": "group",
                "message_type": "post",
                "mentions": [{"id": {"open_id": own_open}}],
            },
        },
    }
    msg = InboundMessage(
        platform="feishu",
        bot_id=receiver.id,
        kind="message",
        chat_type="group",
        chat_id="group",
        sender={"platform_user_id": "", "sender_type": "bot"},
        message_id=mid,
        mentions_bot=True,
        parts=[{"type": "text", "text": "连接测试"}],
        raw=raw,
        reply_context={"chat_id": "group", "message_id": mid},
    )
    assert (
        await enqueue_inbound(
            session,
            SimpleNamespace(id=receiver.id, bot_key=receiver.bot_key, welcome_message=None),
            msg,
            lease_generation=1,
        )
        is None
    )
    await session.flush()


async def test_probe_requires_both_exact_receipts_and_never_starts_ai(db_session, monkeypatch):
    a, b, route, owner, _ = await prepared(db_session, monkeypatch)
    count = await db_session.scalar(select(func.count()).select_from(Task))
    assert route.enabled is False and route.setup["status"] == "pending"
    source = await db_session.get(OutboxItem, route.setup["source_outbox_id"])
    target = await db_session.get(OutboxItem, route.setup["target_outbox_id"])
    await service.guard_probe(db_session, source)
    await service.reconcile(db_session, route)
    assert route.setup["status"] == "pending"
    await receipt(db_session, b, source, "app-b", "open-app-b", "union-a")
    await service.reconcile(db_session, route)
    assert route.setup["status"] == "pending"
    await receipt(db_session, a, target, "app-a", "open-app-a", "union-b")
    await service.reconcile(db_session, route)
    await db_session.flush()
    assert route.setup["status"] == "ready"
    assert route.source_union_id == "union-a" and route.target_union_id == "union-b"
    assert route.tenant_key == "tenant" and not route.enabled
    version = route.version
    await service.reconcile(db_session, route)
    await db_session.flush()
    assert route.version == version
    assert await db_session.scalar(select(func.count()).select_from(Task)) == count


@pytest.mark.parametrize("failure", ["credentials", "actor", "archived", "expired", "cancelled"])
async def test_probe_guard_rejects_revocation_and_stale_configuration(
    db_session, monkeypatch, failure
):
    a, b, route, owner, cipher = await prepared(db_session, monkeypatch)
    item = await db_session.get(OutboxItem, route.setup["source_outbox_id"])
    if failure == "credentials":
        b.credentials_enc = encrypt_json(
            cipher, {"app_id": "app-b", "app_secret": "rotated"}, CREDENTIALS_AAD
        )
    elif failure == "actor":
        owner.status = "disabled"
    elif failure == "archived":
        route.archived = True
    elif failure == "expired":
        route.setup = {
            **route.setup,
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
    else:
        route.setup = {**route.setup, "status": "failed", "reason": "verification_cancelled"}
    await db_session.commit()
    with pytest.raises(ValueError):
        await service.guard_probe(db_session, item)
    assert not (await service.status(db_session, route))["can_enable"]


async def test_probe_rejects_cross_tenant_receipts(db_session, monkeypatch):
    a, b, route, _, _ = await prepared(db_session, monkeypatch)
    source = await db_session.get(OutboxItem, route.setup["source_outbox_id"])
    target = await db_session.get(OutboxItem, route.setup["target_outbox_id"])
    await receipt(db_session, b, source, "app-b", "open-app-b", "union-a")
    await receipt(db_session, a, target, "app-a", "open-app-a", "union-b", tenant="other")
    await service.reconcile(db_session, route)
    assert route.setup["reason"] == "probe_identity_mismatch" and not route.enabled


async def test_setup_gateway_sends_rich_mention_and_records_receipt(db_session, monkeypatch):
    a, _, route, _, _ = await prepared(db_session, monkeypatch)
    item = await db_session.get(OutboxItem, route.setup["source_outbox_id"])
    transport = FeishuTransport(None, None, bot_id=a.id, instance_id="test", generation=1)
    sent = []

    async def send(chat, content, **kwargs):
        sent.append((chat, content, kwargs))
        return "actual-platform-message"

    monkeypatch.setattr(transport, "send", send)
    await transport._send_item(item)
    assert sent[0][1]["zh_cn"]["content"][0] == [{"tag": "at", "user_id": "open-app-b"}]
    assert item.payload["_feishu_message_id"] == "actual-platform-message"


async def test_gateway_skips_probe_from_stale_client_credentials(
    db_session, db_engine, monkeypatch
):
    from coreman.core.db.session import make_session_factory

    a, _, route, _, _ = await prepared(db_session, monkeypatch)
    transport = FeishuTransport(
        make_session_factory(db_engine),
        None,
        bot_id=a.id,
        instance_id="test",
        generation=1,
        credentials_fingerprint="previous-configuration",
    )

    async def unexpected_send(item):
        pytest.fail("stale client must not send")

    monkeypatch.setattr(transport, "_send_item", unexpected_send)
    assert await transport.consume_one()
    item = await db_session.get(OutboxItem, route.setup["source_outbox_id"], populate_existing=True)
    assert item.status == "skipped"


@pytest.mark.parametrize("failure", ["credentials", "owner_revoked", "archived", "offline"])
async def test_verified_route_invalidated_before_real_collaboration(
    db_session, monkeypatch, failure
):
    from coreman.core.chat.bot_collaboration import authorized

    a, b, route, owner, cipher = await prepared(db_session, monkeypatch)
    source = await db_session.get(OutboxItem, route.setup["source_outbox_id"])
    target = await db_session.get(OutboxItem, route.setup["target_outbox_id"])
    await receipt(db_session, b, source, "app-b", "open-app-b", "union-a")
    await receipt(db_session, a, target, "app-a", "open-app-a", "union-b")
    await service.reconcile(db_session, route)
    route.enabled = True
    await db_session.commit()
    human = await db_session.scalar(select(User).where(User.login_name == "human"))
    await authorized(db_session, route, "human-id", human.id)
    if failure == "credentials":
        b.credentials_enc = encrypt_json(
            cipher, {"app_id": "new-app", "app_secret": "test"}, CREDENTIALS_AAD
        )
    elif failure == "owner_revoked":
        owner.status = "disabled"
    elif failure == "archived":
        route.archived = True
    else:
        relay = await db_session.get(RelayServer, a.relay_server_id)
        node = await db_session.get(RuntimeNode, relay.runtime_node_id)
        node.heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()
    with pytest.raises(ValueError):
        await authorized(db_session, route, "human-id", human.id)
