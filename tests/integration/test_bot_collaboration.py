import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from coreman.core.bus import tasks
from coreman.core.chat import bot_collaboration as service
from coreman.core.chat import sessions
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    BotCollaborationRoute,
    OutboxItem,
    Task,
    User,
    UserIdentity,
)
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from tests.integration.test_chat_handler import chat_task
from tests.integration.worker_helpers import seed_bot


async def setup(session):
    a, relay, cipher = await seed_bot(session)
    a.platform = "feishu"
    actor = User(login_name="human", display_name="真实人类")
    session.add(actor)
    await session.flush()
    session.add(UserIdentity(user_id=actor.id, platform="feishu", platform_user_id="human-id"))
    b = Bot(
        bot_key="helper",
        platform="feishu",
        name="B",
        created_by=a.created_by,
        relay_server_id=relay.id,
        model=a.model,
        working_dir="/helper",
        credentials_enc=a.credentials_enc,
        env_vars_enc=a.env_vars_enc,
    )
    session.add(b)
    await session.flush()
    route = BotCollaborationRoute(
        source_bot_id=a.id,
        target_bot_id=b.id,
        chat_id="group",
        tenant_key="tenant",
        source_open_id="oa",
        target_open_id="ob",
        source_union_id="ua",
        target_union_id="ub",
        enabled=True,
        timeout_seconds=300,
    )
    session.add(route)
    await session.commit()
    task = await chat_task(
        session, a, "需要B数据", sender="human-id", chat_type="group", chat_id="group"
    )
    await sessions.get_or_create(
        session,
        bot_id=a.id,
        session_key="group",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=actor.id,
    )
    await session.commit()
    row = await service.request_help(
        session, task_id=task.id, actor=str(actor.id), target_key="helper", question="库存多少?"
    )
    await session.commit()
    return a, b, actor, route, task, row


async def receipt(session, bot, *, mid, union, parent=None, message_type="text"):
    raw = {
        "header": {"tenant_key": "tenant"},
        "event": {
            "sender": {"sender_type": "bot", "sender_id": {"union_id": union}},
            "message": {
                "message_id": mid,
                "chat_id": "group",
                "chat_type": "group",
                "message_type": message_type,
                "parent_id": parent,
            },
        },
    }
    msg = InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="message",
        chat_type="group",
        chat_id="group",
        sender={"platform_user_id": "", "sender_type": "bot"},
        message_id=mid,
        mentions_bot=True,
        parts=[{"type": "text", "text": "反馈"}],
        raw=raw,
        reply_context={"chat_id": "group", "message_id": mid},
    )
    task = await enqueue_inbound(
        session,
        SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None),
        msg,
        lease_generation=1,
    )
    assert task is None
    await session.flush()


@pytest.mark.parametrize("message_type", ["text", "post"])
async def test_real_receipts_required_and_replay_is_idempotent(db_session, message_type):
    a, b, actor, route, task, row = await setup(db_session)
    # Request is idempotent, but cannot change destination/content mid-task.
    assert (
        await service.request_help(
            db_session,
            task_id=task.id,
            actor=str(actor.id),
            target_key="helper",
            question="库存多少?",
        )
    ).id == row.id
    with pytest.raises(ValueError):
        await service.request_help(
            db_session,
            task_id=task.id,
            actor=str(actor.id),
            target_key="helper",
            question="different",
        )
    await service.send_message(db_session, row, route)
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    assert item.payload["_mention_open_id"] == "ob"
    assert item.payload["markdown"] == "库存多少?"
    # Receipt can arrive before the outbound acknowledgement commits.
    await receipt(db_session, b, mid="request", union="ua", message_type=message_type)
    assert await service.tick(db_session, datetime.now(UTC)) == 0
    item.payload = {**item.payload, "_feishu_message_id": "request"}
    item.status = "sent"
    await db_session.flush()
    assert await service.tick(db_session, datetime.now(UTC)) == 1
    assert row.status == "helper_running"
    helper = await db_session.get(Task, row.helper_task_id)
    assert helper.user_id == actor.id and helper.payload["collaboration_phase"] == "helper"
    assert await service.tick(db_session, datetime.now(UTC)) == 0
    with pytest.raises(ValueError):
        await service.request_help(
            db_session,
            task_id=helper.id,
            actor=str(actor.id),
            target_key=a.bot_key,
            question="loop",
        )
    row.response = "库存73，预留11，可用62"
    await service.send_message(db_session, row, route, response=True)
    response = await db_session.get(OutboxItem, row.response_outbox_id)
    assert response.target["message_id"] == "request"
    assert response.payload["_mention_open_id"] == "oa"
    response.payload = {**response.payload, "_feishu_message_id": "reply"}
    response.status = "sent"
    await db_session.flush()
    assert await service.tick(db_session, datetime.now(UTC)) == 0
    await receipt(db_session, a, mid="reply", union="ub", parent="request")
    assert await service.tick(db_session, datetime.now(UTC)) == 1
    resume = await db_session.get(Task, row.resume_task_id)
    assert resume.session_key == row.source_session_key
    assert resume.user_id == actor.id
    await receipt(db_session, a, mid="reply", union="ub", parent="request")
    assert await service.tick(db_session, datetime.now(UTC)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Task)) == 3


@pytest.mark.parametrize(
    "failure", ["timeout", "disabled", "identity", "whitelist", "wrong_peer", "wrong_parent"]
)
async def test_fail_closed(db_session, failure):
    a, b, actor, route, task, row = await setup(db_session)
    await service.send_message(db_session, row, route)
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    item.payload = {**item.payload, "_feishu_message_id": "request"}
    item.status = "sent"
    if failure == "timeout":
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif failure == "disabled":
        b.enabled = False
    elif failure == "identity":
        actor.status = "disabled"
    elif failure == "whitelist":
        db_session.add(BotAllowedUser(bot_id=b.id, user_id=b.created_by))
    elif failure == "wrong_peer":
        await receipt(db_session, b, mid="request", union="unrelated")
    else:
        await receipt(db_session, b, mid="request", union="ua", message_type="post")
        await db_session.flush()
        await service.tick(db_session, datetime.now(UTC))
        row.response = "feedback"
        await service.send_message(db_session, row, route, response=True)
        reply = await db_session.get(OutboxItem, row.response_outbox_id)
        reply.payload = {**reply.payload, "_feishu_message_id": "reply"}
        reply.status = "sent"
        await receipt(db_session, a, mid="reply", union="ub", parent="unrelated")
    await db_session.flush()
    await service.tick(db_session, datetime.now(UTC))
    assert row.status in ("cancelled", "timed_out", "failed")
    assert row.resume_task_id is None
    await service.tick(db_session, datetime.now(UTC))
    notices = list(
        await db_session.scalars(
            select(OutboxItem).where(OutboxItem.dedupe_key == f"collaboration:{row.id}:closed")
        )
    )
    assert len(notices) == 1


async def test_worker_uses_origin_human_and_resumes_original_session(db_engine, db_session):
    from coreman.runtime.worker.chat.collaboration import configure, final_transition, resolve
    from coreman.runtime.worker.chat.models import Verdict
    from coreman.runtime.worker.chat.opening import OpenStage
    from tests.integration.worker_helpers import build_ctx

    a, b, actor, route, task, row = await setup(db_session)
    await service.send_message(db_session, row, route)
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    item.payload = {**item.payload, "_feishu_message_id": "request"}
    item.status = "sent"
    await receipt(db_session, b, mid="request", union="ua", message_type="post")
    await db_session.flush()
    await service.tick(db_session, datetime.now(UTC))
    helper = await db_session.get(Task, row.helper_task_id)
    ctx = build_ctx(db_engine, helper)
    intake, relay, parts = await resolve(db_session, ctx)
    assert intake.speaker.user_id == actor.id
    assert intake.inbound.sender_platform_user_id is None
    assert intake.bot.id == b.id
    assert "库存多少?" in intake.text
    # No delegation capability for B; original task did have one.
    info = sessions.SessionInfo(uuid.uuid4(), True, False)
    prompt, env, turn_context = await configure(db_session, ctx, intake, info, "system", {})
    assert "COREMAN_BOT_HELP_TOKEN" not in env
    helper.status = "running"
    pre = SimpleNamespace(
        writer=SimpleNamespace(
            pending_text='{"status":"completed","answer":"库存73，预留11，可用62"}', boundaries=[]
        )
    )
    verdict = Verdict("success", "succeeded", None, None, "库存73，预留11，可用62")
    verdict, silent = await final_transition(db_session, ctx, pre, verdict)
    assert silent and row.status == "waiting_source"
    item = await db_session.get(OutboxItem, row.response_outbox_id)
    item.payload = {**item.payload, "_feishu_message_id": "reply"}
    item.status = "sent"
    await receipt(db_session, a, mid="reply", union="ub", parent="request")
    await db_session.flush()
    await service.tick(db_session, datetime.now(UTC))
    resumed = await db_session.get(Task, row.resume_task_id)
    resumed_ctx = build_ctx(db_engine, resumed)
    intake, relay, parts = await resolve(db_session, resumed_ctx)
    assert intake.speaker.user_id == actor.id and intake.bot.id == a.id
    assert intake.inbound.id == task.inbound_event_id
    assert "可用62" in intake.text
    info = await OpenStage()._session_info(db_session, resumed_ctx, intake, "claude")
    assert info.relay_session_id == row.source_relay_session_id


async def test_stop_cancels_waiting_and_late_receipt_cannot_resume(db_engine, db_session):
    from coreman.runtime.worker.commands import do_stop
    from tests.integration.worker_helpers import build_ctx

    a, b, actor, route, task, row = await setup(db_session)
    await service.send_message(db_session, row, route)
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    item.payload = {**item.payload, "_feishu_message_id": "request"}
    item.status = "sent"
    await do_stop(db_session, build_ctx(db_engine, task), a.id, "group", "human-id")
    assert row.status == "cancelled"
    await receipt(db_session, b, mid="request", union="ua", message_type="post")
    assert await service.tick(db_session, datetime.now(UTC)) == 0
    assert row.helper_task_id is None


async def test_help_endpoint_auth_binding_and_no_identity_override(db_session):
    import httpx
    from fastapi import FastAPI

    from coreman.api.deps import get_session
    from coreman.api.errors import install_error_handlers
    from coreman.api.routers.bot_collaboration import router
    from coreman.core.crypto import Cipher

    a, b, actor, route, task, row = await setup(db_session)
    app = FastAPI()
    app.include_router(router)
    # use the application's normal error mapping
    install_error_handlers(app)
    app.state.cipher = Cipher(b"x" * 32)

    async def session_override():
        yield db_session

    app.dependency_overrides[get_session] = session_override
    token = service.issue_capability(app.state.cipher, task_id=task.id, user_id=str(actor.id))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {"target_bot_key": "helper", "question": "库存多少?"}
        assert (await client.post("/api/runtime/bot-help", json=body)).status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        ok = await client.post("/api/runtime/bot-help", json=body, headers=headers)
        assert ok.status_code == 200 and ok.json()["collaboration_id"] == str(row.id)
        assert (
            await client.post(
                "/api/runtime/bot-help", json={**body, "actor": str(b.created_by)}, headers=headers
            )
        ).status_code == 422
        task.status = "succeeded"
        await db_session.flush()
        assert (
            await client.post("/api/runtime/bot-help", json=body, headers=headers)
        ).status_code == 409


async def test_direct_human_requests_share_original_group_session(db_session):
    a, b, actor, route, task, row = await setup(db_session)
    queued = []
    for mid in ("human-one", "human-two"):
        msg = InboundMessage(
            platform="feishu",
            bot_id=a.id,
            kind="message",
            chat_type="group",
            chat_id="group",
            sender={"platform_user_id": "human-id"},
            message_id=mid,
            mentions_bot=True,
            parts=[{"type": "text", "text": "check stock"}],
            reply_context={"chat_id": "group", "message_id": mid},
        )
        queued.append(
            await enqueue_inbound(
                db_session,
                SimpleNamespace(id=a.id, bot_key=a.bot_key, welcome_message=None),
                msg,
                lease_generation=1,
            )
        )
    assert queued[0].session_key == queued[1].session_key == "group"
    assert row.status == "cancelled"
    assert queued[0].inbound_event_id != queued[1].inbound_event_id


@pytest.mark.parametrize("action", ["reset", "timeout"])
async def test_close_cancels_source_while_help_is_only_registered(db_engine, db_session, action):
    from coreman.runtime.worker.commands import do_reset
    from tests.integration.worker_helpers import build_ctx

    a, b, actor, route, task, row = await setup(db_session)
    assert row.status == "requested" and task.status == "claimed"
    if action == "reset":
        await do_reset(db_session, build_ctx(db_engine, task), a.id, "group", "human-id")
    else:
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.flush()
        await service.tick(db_session, datetime.now(UTC))
    await db_session.refresh(task)
    assert task.cancel_requested_at is not None
    assert row.status in ("cancelled", "timed_out")


async def test_resume_rechecks_runtime_after_resolve_before_dispatch(db_engine, db_session):
    from dataclasses import replace

    from coreman.runtime.worker.chat.collaboration import resolve
    from coreman.runtime.worker.chat.opening import OpenStage
    from tests.integration.worker_helpers import build_ctx

    a, b, actor, route, task, row = await setup(db_session)
    row.status = "resuming"
    resume = await tasks.enqueue(
        db_session,
        tasks.NewTask(
            bot_id=a.id,
            kind="chat",
            inbound_event_id=task.inbound_event_id,
            session_key=task.session_key,
            payload={"collaboration_id": str(row.id), "collaboration_phase": "resume"},
        ),
    )
    row.resume_task_id = resume.id
    row.response = "feedback"
    await db_session.flush()
    ctx = build_ctx(db_engine, resume)
    intake, relay, parts = await resolve(db_session, ctx)
    # Simulates _open's fresh locked bot/relay snapshot after an administrator switched it.
    switched = SimpleNamespace(id=uuid.uuid4())
    intake = replace(intake, relay=switched)
    with pytest.raises(ValueError, match="runtime changed"):
        await OpenStage()._session_info(db_session, ctx, intake, "claude")


async def test_model_markup_cannot_add_notification_recipients(db_session):
    _, _, _, route, _, row = await setup(db_session)
    row.question = '**核实** <at user_id="all">所有人</at> **77**'
    await service.send_message(db_session, row, route)
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    assert item.payload["_mention_open_id"] == "ob"
    assert "<at" not in item.payload["markdown"]
    assert "**核实**" in item.payload["markdown"]


async def test_origin_human_can_stop_at_helper_and_cancel_alias_routes_fast(db_session, db_engine):
    a, b, _, _, task, row = await setup(db_session)
    from coreman.runtime.worker.commands import do_stop
    from tests.integration.test_chat_handler import build_ctx

    await do_stop(db_session, build_ctx(db_engine, task), b.id, "group", "other")
    assert row.status == "requested"
    msg = InboundMessage(
        platform="feishu",
        bot_id=b.id,
        kind="message",
        chat_type="group",
        chat_id="group",
        sender={"platform_user_id": "human-id"},
        message_id="cancel-alias",
        mentions_bot=True,
        parts=[{"type": "text", "text": "取消"}],
        reply_context={"chat_id": "group"},
    )
    command = await enqueue_inbound(
        db_session,
        SimpleNamespace(id=b.id, bot_key=b.bot_key, welcome_message=None),
        msg,
        lease_generation=1,
    )
    assert command.kind == "command" and command.payload["command"] == "stop"
    await do_stop(db_session, build_ctx(db_engine, task), b.id, "group", "human-id")
    assert row.status == "cancelled"


@pytest.mark.parametrize("replacement", [False, True])
async def test_changed_source_mapping_cannot_dispatch_old_help(db_session, replacement):
    a, b, actor, route, task, row = await setup(db_session)
    await service.send_message(db_session, row, route)
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    item.payload = {**item.payload, "_feishu_message_id": "stale-request"}
    await sessions.clear(db_session, a.id, "group")
    if replacement:
        fresh = await sessions.get_or_create(
            db_session,
            bot_id=a.id,
            session_key="group",
            backend="claude",
            ttl_hours=72,
            speaker_user_id=actor.id,
        )
        assert fresh.relay_session_id != row.source_relay_session_id
    await receipt(db_session, b, mid="stale-request", union="ua")
    await db_session.flush()
    assert await service.tick(db_session, datetime.now(UTC)) == 0
    assert row.status == "cancelled"
    assert row.helper_task_id is None


async def test_serial_claim_waits_for_cancelled_runtime_and_preserves_fifo(db_session):
    a, _, _, _, old, _ = await setup(db_session)
    await tasks.request_cancel(db_session, old.id, "superseded")
    first = await tasks.enqueue(
        db_session,
        tasks.NewTask(
            bot_id=a.id, kind="chat", session_key="group", payload={"serialize_session": True}
        ),
    )
    second = await tasks.enqueue(
        db_session,
        tasks.NewTask(
            bot_id=a.id, kind="chat", session_key="group", payload={"serialize_session": True}
        ),
    )
    assert await tasks.claim(db_session, lane="normal", instance_id="worker-test") is None
    await tasks.finish(db_session, old.id, status="cancelled")
    claimed = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
    assert claimed.id == first.id
    assert await tasks.claim(db_session, lane="normal", instance_id="worker-test") is None
    await tasks.finish(db_session, first.id, status="succeeded")
    claimed = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
    assert claimed.id == second.id


async def test_denied_human_cannot_cancel_shared_collaboration(db_session):
    a, _, actor, _, source, row = await setup(db_session)
    db_session.add(BotAllowedUser(bot_id=a.id, user_id=actor.id))
    await db_session.flush()
    assert not await service.admit_human(db_session, a.id, "group", "outsider")
    assert row.status == "requested"
    await db_session.refresh(source)
    assert source.cancel_requested_at is None


async def test_second_human_replaces_shared_task_without_changing_origin(db_session):
    a, _, actor, _, source, row = await setup(db_session)
    second = User(login_name="second", display_name="第二位人类")
    db_session.add(second)
    await db_session.flush()
    db_session.add(UserIdentity(user_id=second.id, platform="feishu", platform_user_id="second-id"))
    await db_session.flush()
    assert await service.admit_human(db_session, a.id, "group", "second-id")
    assert row.status == "cancelled"
    assert row.origin_user_id == actor.id
    assert row.origin_platform_user_id == "human-id"
    with pytest.raises(ValueError, match="cannot delegate"):
        await service.request_help(
            db_session,
            task_id=source.id,
            actor=str(actor.id),
            target_key="helper",
            question="late registration",
        )


@pytest.mark.parametrize("mutation", ["clear", "replace", "expire"])
async def test_resume_checks_session_after_resolve(db_engine, db_session, mutation):
    from coreman.core.db.models import ChatSession
    from coreman.runtime.worker.chat.collaboration import resolve
    from coreman.runtime.worker.chat.opening import OpenStage
    from tests.integration.worker_helpers import build_ctx

    a, _, actor, _, source, row = await setup(db_session)
    row.status = "resuming"
    resume = await tasks.enqueue(
        db_session,
        tasks.NewTask(
            bot_id=a.id,
            kind="chat",
            inbound_event_id=source.inbound_event_id,
            session_key="group",
            payload={"collaboration_id": str(row.id), "collaboration_phase": "resume"},
        ),
    )
    row.resume_task_id = resume.id
    row.response = "feedback"
    await db_session.flush()
    ctx = build_ctx(db_engine, resume)
    intake, _, _ = await resolve(db_session, ctx)
    mapping = await db_session.get(ChatSession, (a.id, "group"))
    if mutation == "clear":
        await sessions.clear(db_session, a.id, "group")
    elif mutation == "replace":
        mapping.relay_session_id = uuid.uuid4()
    else:
        mapping.last_active_at = datetime.now(UTC) - timedelta(hours=100)
    await db_session.flush()
    with pytest.raises(ValueError, match="session changed or expired"):
        await OpenStage()._session_info(db_session, ctx, intake, "claude")


async def test_two_help_sessions_do_not_reuse_b_daily_session(db_engine, db_session):
    from coreman.runtime.worker.chat.collaboration import resolve
    from coreman.runtime.worker.chat.opening import OpenStage
    from tests.integration.worker_helpers import build_ctx

    a, b, actor, route, source, row = await setup(db_session)
    daily = await sessions.get_or_create(
        db_session,
        bot_id=b.id,
        session_key="group",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=actor.id,
    )
    helper_ids = []
    for number in range(2):
        if number:
            source = await chat_task(
                db_session, a, "再次求助", sender="human-id", chat_type="group", chat_id="group"
            )
            row = await service.request_help(
                db_session,
                task_id=source.id,
                actor=str(actor.id),
                target_key="helper",
                question="只提供本次背景",
            )
        await service.send_message(db_session, row, route)
        item = await db_session.get(OutboxItem, row.request_outbox_id)
        mid = f"help-{number}"
        item.payload = {**item.payload, "_feishu_message_id": mid}
        await receipt(db_session, b, mid=mid, union="ua")
        await db_session.flush()
        await service.tick(db_session, datetime.now(UTC))
        helper = await db_session.get(Task, row.helper_task_id)
        ctx = build_ctx(db_engine, helper)
        intake, _, _ = await resolve(db_session, ctx)
        info = await OpenStage()._session_info(db_session, ctx, intake, "claude")
        helper_ids.append(info.relay_session_id)
        assert helper.session_key == f"collaboration:{row.id}"
        assert info.relay_session_id != daily.relay_session_id
        await tasks.finish(db_session, source.id, status="succeeded")
        await tasks.finish(db_session, helper.id, status="succeeded")
        row.status = "completed"
        await db_session.commit()
    assert helper_ids[0] != helper_ids[1]
    after = await sessions.get_or_create(
        db_session,
        bot_id=b.id,
        session_key="group",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=actor.id,
    )
    assert after.relay_session_id == daily.relay_session_id


async def test_cancel_between_resolve_and_open_does_not_create_session(db_engine, db_session):
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.integration.worker_helpers import build_ctx

    a, _, _, _, source, _ = await setup(db_session)
    handler = ChatTaskHandler()
    ctx = build_ctx(db_engine, source)
    intake, relay, _ = await handler._resolve(db_session, ctx)
    await tasks.request_cancel(db_session, source.id, "superseded")
    await db_session.flush()
    with pytest.raises(ValueError, match="cancelled before dispatch"):
        await handler._open(db_session, ctx, intake, relay, ctx.clock())


@pytest.mark.parametrize("command", ["reset", "stop"])
async def test_denied_fast_command_cannot_mutate_shared_session(db_engine, db_session, command):
    from coreman.core.db.models import ChatSession
    from coreman.runtime.worker.commands import CommandHandler
    from tests.integration.worker_helpers import build_ctx

    a, _, actor, _, source, row = await setup(db_session)
    db_session.add(BotAllowedUser(bot_id=a.id, user_id=actor.id))
    cmd = await tasks.enqueue(
        db_session,
        tasks.NewTask(
            bot_id=a.id,
            kind="command",
            lane="fast",
            session_key="group",
            inbound_event_id=source.inbound_event_id,
            payload={"command": command, "platform_user_id": "outsider"},
        ),
    )
    await db_session.commit()
    await CommandHandler().run(build_ctx(db_engine, cmd))
    await db_session.refresh(row)
    assert row.status == "requested"
    mapping = await db_session.get(ChatSession, (a.id, "group"))
    assert mapping.relay_session_id == row.source_relay_session_id
    await db_session.refresh(cmd)
    assert cmd.result == {"denied": True}


@pytest.mark.parametrize("route_enabled", [True, False])
async def test_configured_peer_does_not_change_ordinary_group_rounds(
    db_engine, db_session, route_enabled
):
    from tests.fakes.fake_relay import FakeRelay
    from tests.integration.test_chat_handler import run

    a, _, actor, route, initial, row = await setup(db_session)
    row.status = "completed"
    route.enabled = route_enabled
    await tasks.finish(db_session, initial.id, status="succeeded")
    await db_session.commit()
    fake = FakeRelay("normal")
    for number in range(2):
        message = InboundMessage(
            platform="feishu",
            bot_id=a.id,
            kind="message",
            chat_type="group",
            chat_id="group",
            sender={"platform_user_id": "human-id"},
            message_id=f"direct-{number}",
            mentions_bot=True,
            parts=[{"type": "text", "text": f"direct turn {number}"}],
            reply_context={"chat_id": "group", "message_id": f"direct-{number}"},
        )
        task = await enqueue_inbound(db_session, a, message, lease_generation=1)
        await db_session.commit()
        claimed = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
        await db_session.commit()
        assert claimed.id == task.id and task.session_key == "group"
        await run(db_engine, claimed, fake)
    assert len(fake.requests) == 2
    assert fake.requests[0]["session_id"] == fake.requests[1]["session_id"]
    assert fake.requests[0]["session_id"] == str(row.source_relay_session_id)
    for request in fake.requests:
        user_text = request["messages"][1]["content"]
        if route_enabled:
            assert "COREMAN_BOT_HELP_URL" in user_text
            assert "ListAgents" in user_text
            assert request["env_vars"]["COREMAN_BOT_HELP_TOKEN"] not in user_text
        else:
            assert "COREMAN_BOT_HELP_URL" not in user_text


async def test_real_stop_ingress_reports_the_work_it_already_cancelled(db_engine, db_session):
    from coreman.core.db.models import TaskStream
    from coreman.core.i18n.messages import msg
    from coreman.runtime.worker.commands import CommandHandler
    from tests.integration.worker_helpers import build_ctx

    a, _, _, _, _, row = await setup(db_session)
    incoming = InboundMessage(
        platform="feishu",
        bot_id=a.id,
        kind="message",
        chat_type="group",
        chat_id="group",
        sender={"platform_user_id": "human-id"},
        message_id="real-stop",
        mentions_bot=True,
        parts=[{"type": "text", "text": "stop"}],
        reply_context={"chat_id": "group", "message_id": "real-stop"},
    )
    command = await enqueue_inbound(db_session, a, incoming, lease_generation=1)
    await db_session.commit()
    assert row.status == "cancelled"
    await tasks.finish(db_session, row.source_task_id, status="cancelled")
    await db_session.commit()
    await CommandHandler().run(build_ctx(db_engine, command))
    stream = await db_session.get(TaskStream, command.id)
    assert stream.final_text == msg("stopped")
    assert "新要求" not in row.error
