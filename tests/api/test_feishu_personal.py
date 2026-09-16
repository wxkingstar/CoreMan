"""Personal Feishu access is private human task scoped, never admin/group scoped."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from coreman.core.bots.secrets import CREDENTIALS_AAD
from coreman.core.db.models import InboundEvent, User, UserIdentity
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import seed_bot

URL = "/api/runtime/feishu-personal/mcp"


async def setup(session, app, *, chat_type="single", platform="feishu"):
    bot, _, _ = await seed_bot(session)
    bot.platform = platform
    bot.credentials_enc = app.state.cipher.encrypt(
        json.dumps({"app_id": "cli_test", "app_secret": "app-secret"}), CREDENTIALS_AAD
    )
    user = User(login_name="personal-reader", display_name="Reader", source="sync")
    session.add(user)
    await session.flush()
    session.add(
        UserIdentity(
            user_id=user.id, platform="feishu", platform_user_id="human", open_id="ou_human"
        )
    )
    task = await chat_task(
        session,
        bot,
        "read my messages",
        sender="human",
        chat_type=chat_type,
        chat_id="oc_private" if chat_type == "single" else "oc_group",
    )
    event = await session.get(InboundEvent, task.inbound_event_id)
    event.sender_open_id = "ou_human"
    event.payload = {
        **event.payload,
        "sender": {**event.payload["sender"], "sender_type": "user"},
        "raw": {
            "header": {"tenant_key": "tenant-test", "app_id": "cli_test"},
            "event": {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"user_id": "human", "open_id": "ou_human"},
                },
                "message": {
                    "chat_type": "p2p" if chat_type == "single" else "group",
                    "chat_id": event.chat_id,
                },
            },
        },
    }
    await session.commit()
    return bot, user, task


def rpc(name, arguments=None):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def value(response):
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


def headers(app, task, user):
    from coreman.core.feishu_personal.policy import issue_capability

    return {
        "Authorization": "Bearer "
        + issue_capability(app.state.cipher, task_id=task.id, user_id=str(user.id))
    }


async def grant(session, app, bot, user, **kw):
    from coreman.core.db.models import FeishuPersonalGrant
    from coreman.core.feishu_personal.policy import app_credentials
    from coreman.core.feishu_personal.service import save_tokens

    app_id, secret = app_credentials(app.state.cipher, bot)
    row = FeishuPersonalGrant(
        bot_id=bot.id,
        user_id=user.id,
        app_id=app_id,
        platform_user_id="human",
        open_id="ou_human",
        tenant_key="tenant-test",
        status="connected",
    )
    session.add(row)
    await session.flush()
    save_tokens(
        app.state.cipher,
        row,
        {
            "access_token": "user-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 7200,
            "scope": "search:message",
        },
        secret,
    )
    for k, v in kw.items():
        setattr(row, k, v)
    await session.commit()
    return row


async def test_missing_capability_is_unauthorized(client):
    assert (await client.post(URL, json=rpc("feishu_authorization_status"))).status_code == 401


@pytest.mark.parametrize(
    "chat_type,platform", [("group", "feishu"), ("single", "wecom"), ("group", "wecom")]
)
async def test_non_private_feishu_provenance_rejected_before_any_http(
    client, app, db_session, chat_type, platform
):
    bot, user, task = await setup(db_session, app, chat_type=chat_type, platform=platform)
    with respx.mock:
        response = await client.post(
            URL, headers=headers(app, task, user), json=rpc("feishu_authorize")
        )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "mutate",
    [
        "foreign_actor",
        "cancelled",
        "finished",
        "scheduled",
        "delegated",
        "bot_sender",
        "no_tenant",
        "no_open_id",
        "disabled",
    ],
)
async def test_capability_cannot_bypass_origin_or_lifecycle(client, app, db_session, mutate):
    bot, user, task = await setup(db_session, app)
    auth = headers(app, task, user)
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    if mutate == "foreign_actor":
        fake = User(id=uuid.uuid4())
        auth = headers(app, task, fake)
    elif mutate == "cancelled":
        task.cancel_requested_at = datetime.now(UTC)
    elif mutate == "finished":
        task.status = "succeeded"
    elif mutate == "scheduled":
        task.kind = "cron_run"
    elif mutate == "delegated":
        task.payload = {**task.payload, "collaboration_id": str(uuid.uuid4())}
    elif mutate == "bot_sender":
        event.payload = {**event.payload, "sender": {"sender_type": "bot"}}
    elif mutate == "no_tenant":
        event.payload = {**event.payload, "raw": {}}
    elif mutate == "no_open_id":
        event.sender_open_id = None
    elif mutate == "disabled":
        user.status = "disabled"
    await db_session.commit()
    with respx.mock:
        r = await client.post(URL, headers=auth, json=rpc("feishu_authorization_status"))
    assert r.status_code == 403


async def test_device_authorization_is_durable_encrypted_and_read_only(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    with respx.mock as mock:
        route = mock.post("https://accounts.feishu.cn/oauth/v1/device_authorization").mock(
            return_value=httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "verification_uri_complete": "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=TEST",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        )
        out = value(
            await client.post(URL, headers=headers(app, task, user), json=rpc("feishu_authorize"))
        )
    assert out["status"] == "pending" and out["authorization_url"].startswith(
        "https://accounts.feishu.cn/"
    )
    from urllib.parse import parse_qs

    scopes = parse_qs(route.calls[0].request.content.decode())["scope"][0]
    assert "search:message" in scopes and "send" not in scopes and "write" not in scopes
    from coreman.core.db.models import FeishuPersonalGrant

    row = (await db_session.scalars(select(FeishuPersonalGrant))).one()
    assert row.pending_enc.startswith("enc:") and "device-secret" not in row.pending_enc
    assert "device-secret" not in json.dumps(out) and row.user_id == user.id


@pytest.mark.parametrize("mismatch", ["user", "tenant", "open_id"])
async def test_authorization_cannot_be_completed_by_different_identity(
    client, app, db_session, mismatch
):
    bot, user, task = await setup(db_session, app)
    with respx.mock as mock:
        mock.post("https://accounts.feishu.cn/oauth/v1/device_authorization").mock(
            return_value=httpx.Response(
                200,
                json={
                    "device_code": "device",
                    "verification_uri_complete": "https://accounts.feishu.cn/verify",
                    "expires_in": 600,
                    "interval": 1,
                },
            )
        )
        value(
            await client.post(URL, headers=headers(app, task, user), json=rpc("feishu_authorize"))
        )
        mock.post("https://open.feishu.cn/open-apis/authen/v2/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "foreign-secret",
                    "refresh_token": "refresh",
                    "expires_in": 7200,
                },
            )
        )
        ident = {"user_id": "human", "open_id": "ou_human", "tenant_key": "tenant-test"}
        ident[{"user": "user_id", "tenant": "tenant_key", "open_id": "open_id"}[mismatch]] = (
            "foreign"
        )
        mock.get("https://open.feishu.cn/open-apis/authen/v1/user_info").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": ident})
        )
        out = value(
            await client.post(
                URL, headers=headers(app, task, user), json=rpc("feishu_authorization_status")
            )
        )
    assert out["error"] == "identity_mismatch"
    from coreman.core.db.models import FeishuPersonalGrant

    row = (await db_session.scalars(select(FeishuPersonalGrant))).one()
    await db_session.refresh(row)
    assert not row.token_enc and not row.pending_enc


async def test_search_returns_real_content_without_exposing_token(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user)
    with respx.mock as mock:
        route = mock.post("https://open.feishu.cn/open-apis/im/v1/messages/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"meta_data": {"message_id": "om_one"}}],
                        "has_more": True,
                        "page_token": "next",
                    },
                },
            )
        )
        mock.get("https://open.feishu.cn/open-apis/im/v1/messages/mget").mock(
            return_value=httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"message_id": "om_one", "body": {"content": "private sample"}}]
                    },
                },
            )
        )
        out = value(
            await client.post(
                URL,
                headers=headers(app, task, user),
                json=rpc("feishu_search_messages", {"query": "budget", "chat_type": "p2p"}),
            )
        )
    assert out["messages"][0]["body"]["content"] == "private sample" and out["has_more"] is True
    assert route.calls[0].request.headers["authorization"] == "Bearer user-secret"
    assert "user-secret" not in json.dumps(out)


async def test_refresh_rotates_durable_token_and_revocation_blocks_reads(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    row = await grant(
        db_session, app, bot, user, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    with respx.mock as mock:
        mock.post("https://open.feishu.cn/open-apis/authen/v2/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "fresh-secret",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 7200,
                },
            )
        )
        route = mock.get("https://open.feishu.cn/open-apis/minutes/v1/minutes/minute-one").mock(
            return_value=httpx.Response(
                200, json={"code": 0, "data": {"minute": {"title": "My meeting"}}}
            )
        )
        mock.get("https://open.feishu.cn/open-apis/minutes/v1/minutes/minute-one/artifacts").mock(
            return_value=httpx.Response(
                200, json={"code": 0, "data": {"transcript": "Speaker: decision"}}
            )
        )
        out = value(
            await client.post(
                URL,
                headers=headers(app, task, user),
                json=rpc("feishu_read_minutes", {"minute_token": "minute-one"}),
            )
        )
    assert out["artifacts"]["transcript"] == "Speaker: decision"
    assert route.calls[0].request.headers["authorization"] == "Bearer fresh-secret"
    out = value(
        await client.post(
            URL, headers=headers(app, task, user), json=rpc("feishu_revoke_authorization")
        )
    )
    assert out["status"] == "revoked"
    with respx.mock:
        out = value(
            await client.post(
                URL,
                headers=headers(app, task, user),
                json=rpc("feishu_read_minutes", {"minute_token": "minute-one"}),
            )
        )
    assert out["error"] == "authorization_required"
    await db_session.refresh(row)
    assert row.token_enc is None


@pytest.mark.parametrize(
    "name,args",
    [
        ("feishu_send_message", {}),
        ("feishu_read_minutes", {"minute_token": "../secrets"}),
        ("feishu_search_messages", {"actor": "foreign"}),
        ("feishu_search_messages", {"limit": 500}),
    ],
)
async def test_no_arbitrary_proxy_or_writes(client, app, db_session, name, args):
    bot, user, task = await setup(db_session, app)
    with respx.mock:
        out = value(await client.post(URL, headers=headers(app, task, user), json=rpc(name, args)))
    assert out["error"] == "invalid_tool_or_arguments"


async def test_app_rotation_requires_new_authorization(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    row = await grant(db_session, app, bot, user)
    bot.credentials_enc = app.state.cipher.encrypt(
        json.dumps({"app_id": "cli_test", "app_secret": "rotated-app-secret"}), CREDENTIALS_AAD
    )
    await db_session.commit()
    with respx.mock:
        out = value(
            await client.post(
                URL,
                headers=headers(app, task, user),
                json=rpc("feishu_read_minutes", {"minute_token": "minute-one"}),
            )
        )
    assert out["error"] == "authorization_required"
    await db_session.refresh(row)
    assert row.token_enc is None


async def test_task_destination_must_match_inbound_private_chat(client, app, db_session):
    _, user, task = await setup(db_session, app)
    task.session_key = "oc_group"
    await db_session.commit()
    with respx.mock:
        response = await client.post(
            URL, headers=headers(app, task, user), json=rpc("feishu_authorize")
        )
    assert response.status_code == 403


async def test_device_poll_interval_is_enforced(client, app, db_session):
    _, user, task = await setup(db_session, app)
    with respx.mock as mock:
        mock.post("https://accounts.feishu.cn/oauth/v1/device_authorization").mock(
            return_value=httpx.Response(
                200,
                json={
                    "device_code": "device",
                    "verification_uri_complete": "https://accounts.feishu.cn/verify",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        )
        value(
            await client.post(URL, headers=headers(app, task, user), json=rpc("feishu_authorize"))
        )
        poll = mock.post("https://open.feishu.cn/open-apis/authen/v2/oauth/token").mock(
            return_value=httpx.Response(400, json={"error": "authorization_pending"})
        )
        for _ in range(2):
            out = value(
                await client.post(
                    URL, headers=headers(app, task, user), json=rpc("feishu_authorization_status")
                )
            )
            assert out["status"] == "pending"
        assert poll.call_count == 1


async def test_refresh_rejection_clears_credentials(client, app, db_session):
    bot, user, task = await setup(db_session, app)
    row = await grant(
        db_session, app, bot, user, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    with respx.mock as mock:
        mock.post("https://open.feishu.cn/open-apis/authen/v2/oauth/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        out = value(
            await client.post(
                URL,
                headers=headers(app, task, user),
                json=rpc("feishu_read_minutes", {"minute_token": "minute-one"}),
            )
        )
    assert out["error"] == "authorization_required"
    await db_session.refresh(row)
    assert row.status == "expired" and row.token_enc is None
