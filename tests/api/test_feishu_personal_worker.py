"""Private mode uses durable provenance and an isolated request capability."""

import uuid

import pytest

from coreman.core.chat import sessions
from coreman.core.chat.identity import resolve_speaker
from coreman.core.db.models import InboundEvent, RelayServer, RuntimeNode
from coreman.core.feishu_personal import policy
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.chat.personal import PRIVATE_POLICY, configure, requested, validate
from tests.api.test_feishu_personal import setup
from tests.integration.worker_helpers import build_ctx


def test_explicit_personal_commands_only():
    for text in ("/飞书个人 看消息", "/personal read messages", "连接我的飞书", "/personal"):
        assert requested(text)
    for text in ("read my messages", "请连接我的飞书", "/personalized", "hello /personal"):
        assert not requested(text)


async def intake_for(db_session, bot, task, text):
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    relay = await db_session.get(RelayServer, bot.relay_server_id)
    node = await db_session.get(RuntimeNode, relay.runtime_node_id)
    node.capabilities = {"claude": {"feishu_personal_restricted_v1": True}}
    await db_session.flush()
    speaker = await resolve_speaker(db_session, platform=bot.platform, platform_user_id="human")
    return Intake(
        bot, relay, event, speaker, event.chat_id, event.chat_type, task.session_key, text, "text"
    )


async def test_private_mode_is_fresh_and_carries_only_dedicated_capability(
    db_session, app, db_engine
):
    bot, user, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "/personal read my messages")
    ctx = build_ctx(db_engine, task)
    original = sessions.SessionInfo(uuid.uuid4(), False, False)
    initial = {
        "COREMAN_FEISHU_PERSONAL_TOKEN": "forged",
        "BOT_TOKEN_ERP": "business-secret",
        "STATIC_SECRET": "other",
        "COREMAN_COLLABORATION_TOKEN": "shared",
    }
    info, prompt, env = await configure(
        db_session, ctx, intake, original, "shared memory prompt", initial
    )
    assert info.relay_session_id != original.relay_session_id and info.is_new
    assert prompt == PRIVATE_POLICY and "shared memory prompt" not in prompt
    assert not {"BOT_TOKEN_ERP", "STATIC_SECRET", "COREMAN_COLLABORATION_TOKEN"} & env.keys()
    assert policy.read_capability(ctx.cipher, env[policy.PREFIX + "TOKEN"]) == (
        task.id,
        str(user.id),
    )
    assert env[policy.PREFIX + "URL"].endswith("/api/runtime/feishu-personal/mcp")
    second, _, _ = await configure(db_session, ctx, intake, info, prompt, env)
    assert second.relay_session_id != info.relay_session_id


async def test_regular_private_chat_never_gets_personal_capability(db_session, app, db_engine):
    bot, _, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "ordinary chat")
    info = sessions.SessionInfo(uuid.uuid4(), False, False)
    result = await configure(
        db_session,
        build_ctx(db_engine, task),
        intake,
        info,
        "ordinary",
        {policy.PREFIX + "TOKEN": "forged", "OTHER": "ok"},
    )
    assert result == (info, "ordinary", {"OTHER": "ok"})


@pytest.mark.parametrize("denial", ["group", "codex", "collaboration", "cron"])
async def test_personal_mode_fails_closed(db_session, app, db_engine, denial):
    bot, _, task = await setup(
        db_session, app, chat_type="group" if denial == "group" else "single"
    )
    if denial == "codex":
        bot.model = "codex/gpt-5"
        relay = await db_session.get(RelayServer, bot.relay_server_id)
        relay.model_provider = "codex"
    if denial == "collaboration":
        task.payload = {**task.payload, "collaboration_phase": "helper"}
    if denial == "cron":
        task.kind = "cron"
    await db_session.commit()
    intake = await intake_for(db_session, bot, task, "/personal read messages")
    with pytest.raises(ValueError):
        await validate(db_session, build_ctx(db_engine, task), intake)


@pytest.mark.parametrize(
    "capability",
    [None, {}, {"feishu_personal_restricted_v1": False}, {"feishu_personal_restricted_v1": "true"}],
)
async def test_old_runtime_never_receives_personal_token(db_session, app, db_engine, capability):
    bot, _, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "/personal read messages")
    node = await db_session.get(RuntimeNode, intake.relay.runtime_node_id)
    node.capabilities = {"claude": capability} if capability else {}
    await db_session.flush()
    with pytest.raises(ValueError, match="升级运行时"):
        await validate(db_session, build_ctx(db_engine, task), intake)


@pytest.mark.parametrize("denial", ["group", "codex", "old_runtime"])
async def test_rejected_command_never_falls_back_to_normal_agent(
    db_session, app, db_engine, denial
):
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes.fake_relay import FakeRelay

    bot, _, task = await setup(
        db_session, app, chat_type="group" if denial == "group" else "single"
    )
    intake = await intake_for(db_session, bot, task, "/personal read messages")
    event = intake.inbound
    event.payload = {
        **event.payload,
        "parts": [{"type": "text", "text": "/personal read messages"}],
    }
    task.payload = {**task.payload, "message": event.payload}
    if denial == "codex":
        intake.relay.model_provider = "codex"
    if denial == "old_runtime":
        node = await db_session.get(RuntimeNode, intake.relay.runtime_node_id)
        node.capabilities = {}
    task.payload = {**task.payload, "message": intake.inbound.payload}
    await db_session.commit()
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    assert fake.requests == []
    await db_session.refresh(task)
    assert task.result == {"personal_mode_denied": True}


async def test_open_stage_builds_isolated_personal_request(db_session, app, db_engine):
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes.fake_relay import FakeRelay

    bot, _, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "/personal read messages")
    intake.inbound.payload = {
        **intake.inbound.payload,
        "parts": [{"type": "text", "text": "/personal read messages"}],
    }
    task.payload = {**task.payload, "message": intake.inbound.payload}
    await db_session.commit()
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: fake.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request["messages"][0]["content"] == PRIVATE_POLICY
    assert request["env_vars"][policy.PREFIX + "TOKEN"]
    assert not any(key.startswith("BOT_TOKEN_") for key in request["env_vars"])
