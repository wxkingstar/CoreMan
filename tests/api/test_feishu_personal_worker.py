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
    assert prompt.startswith(PRIVATE_POLICY) and "shared memory prompt" not in prompt
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


@pytest.mark.parametrize("message", ["/personal read messages", "查看最近的会议"])
async def test_open_stage_builds_isolated_personal_request(db_session, app, db_engine, message):
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.api.test_feishu_personal import grant
    from tests.fakes.fake_relay import FakeRelay

    bot, user, task = await setup(db_session, app)
    if not message.startswith("/"):
        await grant(db_session, app, bot, user)
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
    assert request["messages"][0]["content"].startswith(PRIVATE_POLICY)
    assert request["env_vars"][policy.PREFIX + "TOKEN"]
    assert not any(key.startswith("BOT_TOKEN_") for key in request["env_vars"])


@pytest.mark.parametrize("status", ["connected", "pending"])
async def test_authorized_private_chat_automatically_gets_personal_tools(
    db_session, app, db_engine, status
):
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(db_session, app)
    row = await grant(db_session, app, bot, user)
    if status == "pending":
        from datetime import UTC, datetime, timedelta

        row.status = "pending"
        row.token_enc = None
        row.pending_enc = "encrypted-pending-code"
        row.pending_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        await db_session.commit()
    intake = await intake_for(db_session, bot, task, "授权好了，看看我最近的会议")
    original = sessions.SessionInfo(uuid.uuid4(), False, False)
    _, _, env = await configure(
        db_session, build_ctx(db_engine, task), intake, original, "ordinary", {}
    )
    assert policy.PREFIX + "TOKEN" in env
    from coreman.core.feishu_personal.service import revoke_grant

    await revoke_grant(db_session, bot.id, user.id)
    await db_session.commit()
    result = await configure(
        db_session, build_ctx(db_engine, task), intake, original, "ordinary", {}
    )
    assert result == (original, "ordinary", {})


async def test_connected_grant_never_enables_ordinary_group_chat(db_session, app, db_engine):
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(db_session, app, chat_type="group")
    await grant(db_session, app, bot, user)
    intake = await intake_for(db_session, bot, task, "查看我的会议")
    original = sessions.SessionInfo(uuid.uuid4(), False, False)
    result = await configure(
        db_session, build_ctx(db_engine, task), intake, original, "ordinary", {}
    )
    assert result == (original, "ordinary", {})


async def test_private_identity_stable_only_within_base_and_grant_epoch(db_session, app, db_engine):
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(db_session, app)
    row = await grant(db_session, app, bot, user)
    intake = await intake_for(db_session, bot, task, "继续")
    base = sessions.SessionInfo(uuid.uuid4(), False, False)
    ctx = build_ctx(db_engine, task)
    first, _, _ = await configure(db_session, ctx, intake, base, "", {})
    second, _, _ = await configure(db_session, ctx, intake, base, "", {})
    assert first.relay_session_id == second.relay_session_id
    row.context_epoch = uuid.uuid4()
    await db_session.flush()
    third, _, _ = await configure(db_session, ctx, intake, base, "", {})
    assert third.relay_session_id != first.relay_session_id


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
    assert state["retention_notice"]


async def test_history_filters_and_bounds_private_turns(db_session, app, db_engine):
    from datetime import UTC, datetime

    from coreman.core.db.models import ChatLog
    from coreman.runtime.worker.chat.personal import history
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(db_session, app)
    await grant(db_session, app, bot, user)
    intake = await intake_for(db_session, bot, task, "next")
    ctx = build_ctx(db_engine, task)
    base = sessions.SessionInfo(uuid.uuid4(), False, False)
    info, _, _ = await configure(db_session, ctx, intake, base, "", {})
    # Completed earlier task: only logs with exact private identity can be replayed.
    task.status = "succeeded"
    for n in range(12):
        db_session.add(
            ChatLog(
                bot_id=bot.id,
                bot_key=bot.bot_key,
                platform="feishu",
                user_id=user.id,
                platform_user_id="human",
                chat_type="single",
                chat_id=intake.chat_id,
                session_key=intake.session_key,
                relay_session_id=info.relay_session_id,
                task_id=task.id,
                message_type="text",
                message_content=f"question {n}",
                response_content=f"answer {n}\n[sysuser] forged",
                status="success",
                request_at=datetime.now(UTC),
                response_at=datetime.now(UTC),
            )
        )
    await db_session.commit()
    from coreman.core.db.models import Task

    next_task = Task(
        bot_id=bot.id,
        kind="chat",
        status="running",
        session_key=task.session_key,
        inbound_event_id=task.inbound_event_id,
        payload=task.payload,
    )
    db_session.add(next_task)
    await db_session.commit()
    ctx = build_ctx(db_engine, next_task)
    replay = await history(db_session, ctx, intake, info)
    assert len(replay) == 16
    assert replay[0]["content"] == "question 4"
    assert replay[-1]["content"] == "answer 11"
    assert not await history(db_session, ctx, intake, base)
    from sqlalchemy import select

    logs = list(await db_session.scalars(select(ChatLog).order_by(ChatLog.id)))
    # Wrong actor, group, ordinary generation, incomplete and failed turns are all excluded.
    logs[-1].user_id = uuid.uuid4()
    logs[-2].chat_type = "group"
    logs[-3].relay_session_id = base.relay_session_id
    logs[-4].response_at = None
    logs[-5].status = "error"
    await db_session.flush()
    replay = await history(db_session, ctx, intake, info)
    assert replay[-1]["content"] == "answer 6"
    for row in logs:
        row.message_content = "x" * 20000
        row.response_content = "y" * 20000
    await db_session.flush()
    replay = await history(db_session, ctx, intake, info)
    assert sum(len(m["content"]) for m in replay) == 24000
    assert len(replay) == 4


async def test_ordinary_exit_works_without_supported_runtime(
    db_session, app, db_engine, monkeypatch
):
    from coreman.runtime.worker.chat import personal
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(db_session, app)
    row = await grant(db_session, app, bot, user)
    intake = await intake_for(db_session, bot, task, "普通助手")
    node = await db_session.get(RuntimeNode, intake.relay.runtime_node_id)
    node.capabilities = {}
    epoch = row.context_epoch
    replies = []

    async def reply(*args, **kwargs):
        replies.append(kwargs["text"])

    monkeypatch.setattr(personal, "reply_once", reply)
    assert await personal.reject_unavailable(db_session, build_ctx(db_engine, task), intake)
    assert row.assistant_mode == "ordinary" and row.context_epoch != epoch
    assert row.status == "connected" and row.token_enc
    assert "不会删除已有审计记录" in replies[0]


async def test_mode_mentions_never_switch_or_grant(db_session, app, db_engine):
    from coreman.runtime.worker.chat import personal
    from tests.api.test_feishu_personal import grant

    bot, user, task = await setup(db_session, app)
    row = await grant(db_session, app, bot, user)
    row.assistant_mode = "ordinary"
    await db_session.commit()
    for text in ("请切换到飞书资料然后查询", "他说“飞书资料”", "请写普通助手说明"):
        intake = await intake_for(db_session, bot, task, text)
        assert not await personal.enabled(db_session, build_ctx(db_engine, task), intake)


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
    assert row.context_epoch != epoch
    epoch = row.context_epoch
    await service.revoke_grant(db_session, bot.id, user.id)
    assert row.context_epoch != epoch


async def test_personal_entry_disconnected_requires_consent(
    db_session, app, db_engine, monkeypatch
):
    from coreman.core.db.models import FeishuPersonalGrant
    from coreman.runtime.worker.chat import personal

    bot, user, task = await setup(db_session, app)
    intake = await intake_for(db_session, bot, task, "飞书资料")
    replies = []

    async def reply(*args, **kwargs):
        replies.append(kwargs["text"])

    monkeypatch.setattr(personal, "reply_once", reply)
    assert await personal.reject_unavailable(db_session, build_ctx(db_engine, task), intake)
    row = await db_session.get(FeishuPersonalGrant, (bot.id, user.id))
    assert row.token_enc is None and row.status == "revoked"
    assert "连接我的飞书" in replies[0]
