"""Personal tools are added to the ordinary assistant, only in verified private chats."""

import uuid

import pytest

from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import InboundEvent, RelayServer, RuntimeNode
from coreman.core.feishu_personal import policy
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.chat.personal import configure, connect_requested, intercept
from tests.api.test_feishu_personal import setup
from tests.integration.worker_helpers import build_ctx

SUPPORTED = {"claude": {"feishu_personal_tools_v1": True}}


def test_only_connect_commands_are_reserved():
    # 飞书斜杠指令 /connect 等同于「连接飞书」，选中后末尾常带空格。
    for text in (" 连接飞书 ", "连接我的飞书", "/connect", "/connect ", "/Connect"):
        assert connect_requested(text)
    for text in (
        "connect",
        "/connect me",
        "//connect",
        "请连接我的飞书",
        "普通助手",
        "飞书资料",
        "/personal read messages",
    ):
        assert not connect_requested(text)


async def intake_for(db_session, bot, task, text, capabilities=SUPPORTED):
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    relay = await db_session.get(RelayServer, bot.relay_server_id)
    node = await db_session.get(RuntimeNode, relay.runtime_node_id)
    node.capabilities = capabilities
    await db_session.flush()
    speaker = await resolve_speaker(db_session, platform=bot.platform, platform_user_id="human")
    return Intake(
        bot, relay, event, speaker, event.chat_id, event.chat_type, task.session_key, text, "text"
    )


async def test_private_chat_keeps_the_assistant_and_adds_personal_tools(db_session, app, db_engine):
    bot, user, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "今天的日报写好了吗")
    ctx = build_ctx(db_engine, task)
    base = uuid.uuid4()
    initial = {
        policy.PREFIX + "TOKEN": "forged",
        "BOT_TOKEN_ERP": "business-token",
        "STATIC_SECRET": "bot-secret",
    }
    prompt, env = await configure(db_session, ctx, intake, base, "你是销售", initial)
    assert prompt.startswith("你是销售")
    assert "## 本人飞书" in prompt and "连接飞书" in prompt and "## 本人定时任务" in prompt
    assert env["BOT_TOKEN_ERP"] == "business-token" and env["STATIC_SECRET"] == "bot-secret"
    assert env[policy.PREFIX + "URL"].endswith("/api/runtime/feishu-personal/mcp")
    capability = policy.read_capability(ctx.cipher, env[policy.PREFIX + "TOKEN"])
    assert (capability.task_id, capability.actor) == (task.id, str(user.id))
    assert capability.session_id == base and capability.epoch == policy.NO_GRANT_EPOCH
    # 没连接过的人不留授权记录。
    from coreman.core.db.models import FeishuPersonalGrant

    assert await db_session.get(FeishuPersonalGrant, (bot.id, user.id)) is None


async def test_connected_grant_is_described_and_bound_to_its_generation(db_session, app, db_engine):
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(db_session, app)
    row = await grant(db_session, app, bot, user)
    intake = await intake_for(db_session, bot, task, "看看我最近的会议")
    ctx = build_ctx(db_engine, task)
    prompt, env = await configure(db_session, ctx, intake, uuid.uuid4(), "", {})
    assert "授权范围" in prompt and "不可信的外部资料" in prompt
    assert policy.read_capability(ctx.cipher, env[policy.PREFIX + "TOKEN"]).epoch == (
        row.context_epoch
    )


@pytest.mark.parametrize("denial", ["group", "collaboration", "unbound", "wecom"])
async def test_no_personal_tools_outside_verified_private_chats(db_session, app, db_engine, denial):
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(
        db_session, app, chat_type="group" if denial == "group" else "single"
    )
    await grant(db_session, app, bot, user)
    if denial == "collaboration":
        task.payload = {**task.payload, "collaboration_phase": "helper"}
    if denial == "wecom":
        bot.platform = "wecom"
    await db_session.commit()
    intake = await intake_for(db_session, bot, task, "查看我的会议")
    if denial == "unbound":
        from dataclasses import replace

        from coreman.core.prompting import Speaker

        intake = replace(intake, speaker=Speaker("human", None, None, None))
    initial = {policy.PREFIX + "TOKEN": "forged", "OTHER": "ok"}
    result = await configure(
        db_session, build_ctx(db_engine, task), intake, uuid.uuid4(), "ordinary", initial
    )
    assert result == ("ordinary", {"OTHER": "ok"})


@pytest.mark.parametrize(
    "capabilities",
    [
        {},
        {"claude": {}},
        # The former replacement-mode flag does not make a runtime additive.
        {"claude": {"feishu_personal_restricted_v1": True}},
        {"claude": {"feishu_personal_tools_v1": "true"}},
        {"codex": {"feishu_personal_tools_v1": True}},
    ],
)
async def test_runtime_without_additive_tools_keeps_plain_assistant(
    db_session, app, db_engine, capabilities
):
    bot, _, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "hello", capabilities)
    result = await configure(
        db_session, build_ctx(db_engine, task), intake, uuid.uuid4(), "ordinary", {"A": "b"}
    )
    assert result == ("ordinary", {"A": "b"})


async def test_codex_runtime_gets_tools_when_it_declares_them(db_session, app, db_engine):
    bot, _, task = await setup(db_session, app)
    relay = await db_session.get(RelayServer, bot.relay_server_id)
    relay.model_provider = "codex"
    await db_session.commit()
    intake = await intake_for(
        db_session, bot, task, "hello", {"codex": {"feishu_personal_tools_v1": True}}
    )
    _, env = await configure(db_session, build_ctx(db_engine, task), intake, uuid.uuid4(), "", {})
    assert policy.PREFIX + "TOKEN" in env


async def test_open_stage_sends_the_normal_request_with_tools(db_session, app, db_engine):
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.api.test_feishu_personal import grant
    from tests.fakes.fake_relay import FakeRelay

    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user)
    message = "查看最近的会议"
    intake = await intake_for(db_session, bot, task, message)
    intake.inbound.payload = {
        **intake.inbound.payload,
        "parts": [{"type": "text", "text": message}],
    }
    task.payload = {**task.payload, "message": intake.inbound.payload}
    await db_session.commit()
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    assert len(fake.requests) == 1
    request = fake.requests[0]
    system = request["messages"][0]["content"]
    assert "你是销售" in system and "## 本人飞书" in system
    assert [m["role"] for m in request["messages"]] == ["system", "user"]
    assert request["env_vars"][policy.PREFIX + "TOKEN"]
    assert request["env_vars"]["COREMAN_USER_LOGIN"] == user.login_name
    # The ordinary chat session, not a separate private one.
    from coreman.core.db.models import ChatSession

    base = await db_session.get(ChatSession, (bot.id, task.session_key), populate_existing=True)
    assert request["session_id"] == str(base.relay_session_id)
    await db_session.refresh(task)
    assert not (task.result or {}).get("feishu_personal")


@pytest.mark.parametrize("text", ["普通助手", "飞书资料", "/personal read messages"])
async def test_former_mode_words_are_ordinary_messages(db_session, app, db_engine, text):
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(db_session, app)
    row = await grant(db_session, app, bot, user)
    epoch = row.context_epoch
    intake = await intake_for(db_session, bot, task, text)
    assert not await intercept(db_session, build_ctx(db_engine, task), intake)
    await db_session.refresh(row)
    assert row.context_epoch == epoch and row.status == "connected"


@pytest.mark.parametrize("denial", ["group", "old_runtime", "wecom"])
async def test_connect_outside_supported_private_chat_explains_and_stops(
    db_session, app, db_engine, monkeypatch, denial
):
    from coreman.core.bus import tasks
    from coreman.runtime.worker.chat import personal

    bot, _, task = await setup(
        db_session, app, chat_type="group" if denial == "group" else "single"
    )
    if denial == "wecom":
        bot.platform = "wecom"
        await db_session.commit()
    intake = await intake_for(
        db_session, bot, task, "连接飞书", {} if denial == "old_runtime" else SUPPORTED
    )
    replies = []

    async def reply(*args, **kwargs):
        replies.append(kwargs["text"])

    monkeypatch.setattr(personal, "reply_once", reply)
    handled = await intercept(db_session, build_ctx(db_engine, task), intake)
    if denial == "wecom":
        # 企微机器人没有飞书授权；这句话按普通对话交给助手。
        assert not handled and not replies
        return
    assert handled and replies
    assert ("升级运行时" in replies[0]) == (denial == "old_runtime")
    await db_session.commit()
    finished = await tasks.get(db_session, task.id)
    assert finished.result == {"personal_connect_denied": True}


async def test_expiry_is_authoritative_not_grant_disconnection(db_session, app):
    from datetime import UTC, datetime, timedelta

    from coreman.core.feishu_personal import service
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    state = await service.authorization_status(db_session, app.state.cipher, scope)
    assert state["status"] == "connected"
    assert state["access_token_expired"] is True
    assert state["refresh_available"] is True
    assert state["retention_notice"] and "assistant_mode" not in state


async def test_selection_revoke_and_reauthorization_clear_but_refresh_preserves_epoch(
    db_session, app, monkeypatch
):
    from coreman.core.feishu_personal import service
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(db_session, app)
    row = await grant(db_session, app, bot, user)
    scope = await policy.task_scope(db_session, task.id, str(user.id))
    epoch = row.context_epoch
    _, secret = policy.app_credentials(app.state.cipher, bot)
    service.save_tokens(
        app.state.cipher, row, {"access_token": "refreshed", "refresh_token": "r"}, secret
    )
    assert row.context_epoch == epoch

    async def revoke(*args):
        return True

    monkeypatch.setattr(service, "_revoke_remote", revoke)
    await service.begin_selection(db_session, app.state.cipher, scope)
    assert row.context_epoch != epoch
    epoch = row.context_epoch
    service.save_tokens(app.state.cipher, row, {"access_token": "new"}, secret)
    assert row.context_epoch == epoch
    epoch = row.context_epoch
    await service.revoke_grant(db_session, bot.id, user.id)
    assert row.context_epoch != epoch


async def _tool_names(client, auth):
    response = await client.post(
        "/api/runtime/feishu-personal/mcp",
        headers=auth,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200, response.text
    return {tool["name"] for tool in response.json()["result"]["tools"]}


async def _without_grant(app, task, user):
    """What the worker issues to someone who never connected: no grant row is created."""
    from coreman.core.chat import sessions

    async with app.state.session_factory() as session:
        info = await sessions.get_or_create(
            session,
            bot_id=task.bot_id,
            session_key=task.session_key,
            backend="claude",
            ttl_hours=72,
            speaker_user_id=user.id,
        )
        await session.commit()
    token = policy.issue_capability(
        app.state.cipher,
        task_id=task.id,
        user_id=str(user.id),
        context_epoch=policy.NO_GRANT_EPOCH,
        base_session_id=info.relay_session_id,
    )
    return {"Authorization": "Bearer " + token}


async def test_tool_list_follows_the_grant(db_session, app, client):
    from datetime import UTC, datetime, timedelta

    from coreman.core.db.models import FeishuPersonalGrant
    from tests.api.test_feishu_personal import grant, headers

    bot, user, task = await setup(db_session, app)
    names = await _tool_names(client, await _without_grant(app, task, user))
    assert await db_session.get(FeishuPersonalGrant, (bot.id, user.id)) is None
    assert {"feishu_authorize", "schedule_propose", "schedule_list"} <= names
    assert "feishu_search_messages" not in names
    row = await grant(db_session, app, bot, user)
    names = await _tool_names(client, await headers(app, task, user))
    assert {"feishu_search_messages", "feishu_authorize", "schedule_propose"} <= names
    assert "feishu_send_message" not in names
    row.status, row.token_enc = "pending", None
    row.pending_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()
    assert "feishu_search_messages" in await _tool_names(client, await headers(app, task, user))


@pytest.mark.parametrize("change", ["epoch", "revoke", "reset"])
async def test_capability_fenced_by_context_changes(db_session, app, client, change):
    from coreman.core.db.models import ChatSession
    from coreman.core.feishu_personal import service
    from tests.api.test_feishu_personal import URL, grant, headers, rpc, value

    bot, user, task = await setup(db_session, app)
    row = await grant(db_session, app, bot, user)
    old = await headers(app, task, user)
    if change == "epoch":
        row.context_epoch = uuid.uuid4()
    elif change == "reset":
        base = await db_session.get(ChatSession, (bot.id, task.session_key))
        await db_session.delete(base)
    else:
        await service.revoke_grant(db_session, bot.id, user.id)
    await db_session.commit()
    response = await client.post(URL, headers=old, json=rpc("feishu_authorization_status"))
    if change == "reset":
        assert response.status_code == 403
        return
    # 授权换代只让飞书工具失效，本人定时任务工具照常可用。
    assert value(response) == {"error": "authorization_changed"}
    assert "feishu_authorize" not in await _tool_names(client, old)
    assert "items" in value(await client.post(URL, headers=old, json=rpc("schedule_list")))
    fresh = await headers(app, task, user)
    assert "error" not in value(
        await client.post(URL, headers=fresh, json=rpc("feishu_authorization_status"))
    )


async def test_missing_epoch_capability_is_rejected(db_session, app, client):
    import json
    import time

    from tests.api.test_feishu_personal import URL, rpc

    _, user, task = await setup(db_session, app)
    token = app.state.cipher.encrypt(
        json.dumps({"task": task.id, "actor": str(user.id), "exp": time.time() + 1000}), policy.AAD
    )
    response = await client.post(
        URL, headers={"Authorization": "Bearer " + token}, json=rpc("feishu_authorization_status")
    )
    assert response.status_code == 401


async def test_chat_capability_cannot_name_a_scheduled_task(db_session, app, client):
    from tests.api.test_feishu_personal import URL, rpc

    _, user, task = await setup(db_session, app)
    token = policy.issue_capability(
        app.state.cipher,
        task_id=task.id,
        user_id=str(user.id),
        context_epoch=policy.NO_GRANT_EPOCH,
        base_session_id=None,
    )
    response = await client.post(
        URL, headers={"Authorization": "Bearer " + token}, json=rpc("schedule_list")
    )
    assert response.status_code == 403


async def test_verified_oauth_completion_preserves_new_attempt_capability(
    db_session, app, client, monkeypatch
):
    from coreman.core.feishu_personal import service
    from tests.api.test_feishu_personal import URL, grant, headers, rpc, value

    bot, user, task = await setup(db_session, app)
    row = await grant(db_session, app, bot, user)
    old_attempt = await headers(app, task, user)
    # Selection has already cleared the old authorization generation.
    service._clear(row, "pending")
    await db_session.commit()
    before_link = await headers(app, task, user)
    _, secret = policy.app_credentials(app.state.cipher, bot)

    async def device(*args, **kwargs):
        return {
            "device_code": "new-attempt",
            "verification_uri_complete": "https://accounts.feishu.cn/verify",
        }

    monkeypatch.setattr(service, "_http", device)
    await service._start_authorization(row, secret, app.state.cipher)
    await db_session.commit()
    capability = await headers(app, task, user)
    epoch = row.context_epoch
    service.save_tokens(app.state.cipher, row, {"access_token": "verified-new-token"}, secret)
    await db_session.commit()
    assert row.context_epoch == epoch
    response = await client.post(URL, headers=capability, json=rpc("feishu_authorization_status"))
    assert "error" not in value(response)
    for stale in (old_attempt, before_link):
        response = await client.post(URL, headers=stale, json=rpc("feishu_authorization_status"))
        assert value(response) == {"error": "authorization_changed"}


async def test_session_link_rules_by_chat_type_and_identity(db_session, app, db_engine):
    from dataclasses import replace

    from coreman.core.prompting import Speaker
    from coreman.runtime.worker.chat.opening import session_link

    bot, user, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "hi")
    ctx = build_ctx(db_engine, task)
    sid = uuid.uuid4()
    plain = f"/claude/session/{sid}"
    link = await session_link(db_session, ctx, intake, intake.relay, sid)
    assert "?t=" in link
    from urllib.parse import parse_qs, urlsplit

    from coreman.core import session_links

    claims = session_links.read(ctx.cipher, parse_qs(urlsplit(link).query)["t"][0])
    assert claims is not None and claims.user_id == user.id and claims.session_id == sid
    group = replace(intake, chat_type="group")
    assert (await session_link(db_session, ctx, group, intake.relay, sid)).endswith(plain)
    # 没绑定员工身份的飞书私聊谁也打不开，不给入口；企微私聊仍留给管理员查看。
    anonymous = replace(intake, speaker=Speaker("human", None, None, None))
    assert await session_link(db_session, ctx, anonymous, intake.relay, sid) == ""
    bot.platform = "wecom"
    assert (await session_link(db_session, ctx, anonymous, intake.relay, sid)).endswith(plain)
