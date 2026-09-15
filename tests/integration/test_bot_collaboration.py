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


async def receipt(session, bot, *, mid, union, parent=None):
    raw = {
        "header": {"tenant_key": "tenant"},
        "event": {
            "sender": {"sender_type": "bot", "sender_id": {"union_id": union}},
            "message": {
                "message_id": mid,
                "chat_id": "group",
                "chat_type": "group",
                "message_type": "text",
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


async def test_real_receipts_required_and_replay_is_idempotent(db_session):
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
    await service.send_text(db_session, row, route)
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    assert item.payload["text"].startswith('<at user_id="ob">')
    # Receipt can arrive before the outbound acknowledgement commits.
    await receipt(db_session, b, mid="request", union="ua")
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
    await service.send_text(db_session, row, route, response=True)
    response = await db_session.get(OutboxItem, row.response_outbox_id)
    assert response.target["message_id"] == "request"
    assert '<at user_id="oa">' in response.payload["text"]
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
    await service.send_text(db_session, row, route)
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
        await receipt(db_session, b, mid="request", union="ua")
        await db_session.flush()
        await service.tick(db_session, datetime.now(UTC))
        row.response = "feedback"
        await service.send_text(db_session, row, route, response=True)
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
    await service.send_text(db_session, row, route)
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    item.payload = {**item.payload, "_feishu_message_id": "request"}
    item.status = "sent"
    await receipt(db_session, b, mid="request", union="ua")
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
    prompt, env = await configure(db_session, ctx, intake, info, "system", {})
    assert "COREMAN_BOT_HELP_TOKEN" not in env
    helper.status = "running"
    pre = SimpleNamespace(writer=SimpleNamespace(pending_text="库存73，预留11，可用62"))
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
    await service.send_text(db_session, row, route)
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    item.payload = {**item.payload, "_feishu_message_id": "request"}
    item.status = "sent"
    await do_stop(db_session, build_ctx(db_engine, task), a.id, "group", "human-id")
    assert row.status == "cancelled"
    await receipt(db_session, b, mid="request", union="ua")
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


async def test_parallel_human_requests_get_distinct_sessions(db_session):
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
    assert queued[0].session_key != queued[1].session_key
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
