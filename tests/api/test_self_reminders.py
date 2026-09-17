"""Fixed reminders must never inherit AI capabilities or another human's identity."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from coreman.core.db.models import CronJob, InboundEvent, InteractionState, OutboxItem, UserIdentity
from coreman.core.reminders import handle_request, parse_request
from tests.api.test_feishu_personal import setup


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("两分钟后提醒我检查接口，只提醒一次", 120),
        ("1小时后提醒我喝水", 3600),
        ("三天后提醒我复查", 259200),
    ],
)
def test_bounded_relative_parser(text, seconds):
    assert parse_request(text)[0] == seconds


@pytest.mark.parametrize(
    "text",
    [
        "每天提醒我检查接口",
        "明天提醒我检查接口",
        "两分钟后提醒小王检查接口",
        "他说：两分钟后提醒我检查接口",
        "“两分钟后提醒我检查接口”",
        "零分钟后提醒我检查接口",
        "999天后提醒我检查接口",
    ],
)
def test_ambiguous_and_quoted_requests_are_not_parsed(text):
    assert parse_request(text) is None


async def direct(session, app, text):
    bot, user, task = await setup(session, app)
    await set_text(session, task, text)
    return bot, user, task


async def set_text(session, task, text):
    event = await session.get(InboundEvent, task.inbound_event_id)
    payload = copy.deepcopy(event.payload)
    payload["parts"] = [{"type": "text", "text": text}]
    payload["sender"]["open_id"] = "ou_human"
    payload["raw"]["header"]["event_type"] = "im.message.receive_v1"
    payload["raw"]["event"]["message"].update(
        message_id=event.platform_msg_id, message_type="text", content=json.dumps({"text": text})
    )
    event.payload = payload
    task.payload = {"message": payload}
    await session.commit()


async def test_propose_confirm_once_and_original_identity(db_session, app):
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口，只提醒一次")
    now = datetime.now(UTC)
    reply = await handle_request(db_session, task, app.state.cipher, now=now)
    assert "确认提醒" in reply and "北京时间" in reply and "检查接口" in reply
    assert await db_session.scalar(select(CronJob)) is None
    await set_text(db_session, task, "确认提醒")
    reply = await handle_request(db_session, task, app.state.cipher, now=now)
    assert "已设置" in reply
    await db_session.commit()
    job = await db_session.scalar(select(CronJob))
    assert job.execution_mode == "self_reminder" and job.created_by == user.id
    assert job.run_at == now + timedelta(minutes=2) and job.target_users == [user.id]
    assert job.prompt == "检查接口" and job.target_chats == [] and job.system_prompt is None
    assert "没有待确认" in await handle_request(db_session, task, app.state.cipher, now=now)
    assert len((await db_session.scalars(select(CronJob))).all()) == 1


@pytest.mark.parametrize(
    "text",
    [
        "帮我写一份温和的缴费提醒通知",
        "总结会议中的提醒事项",
        "帮我把“明天提醒我交水费”翻译成英文",
        "请分析‘两分钟后提醒我检查接口’这句话",
        "确认提醒这四个字翻译成英文",
        "取消提醒用英语怎么说？",
    ],
)
@pytest.mark.parametrize("chat_type", ["single", "group"])
async def test_plain_reminder_mentions_continue_to_the_model_path(db_session, app, text, chat_type):
    _, _, task = await direct(db_session, app, text)
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    event.chat_type = chat_type
    await db_session.commit()
    assert await handle_request(db_session, task, app.state.cipher) is None
    assert await db_session.scalar(select(InteractionState)) is None


async def test_explicit_but_unsupported_reminder_request_keeps_help(db_session, app):
    _, _, task = await direct(db_session, app, "明天提醒我交水费")
    assert "支持本人私聊的一次性提醒" in await handle_request(db_session, task, app.state.cipher)


@pytest.mark.parametrize(
    "text",
    ["明天提醒我交水费", "请明天提醒我交水费", "麻烦你下周提醒我续费"],
)
async def test_explicit_unsupported_reminder_forms_still_enter_fixed_flow(db_session, app, text):
    _, _, task = await direct(db_session, app, text)
    assert "支持本人私聊的一次性提醒" in await handle_request(db_session, task, app.state.cipher)


@pytest.mark.parametrize(
    "tamper",
    [
        "quote",
        "app",
        "chat",
        "bot_sender",
        "disabled_user",
        "disabled_bot",
        "task_kind",
        "payload",
        "raw_content",
    ],
)
async def test_proof_fails_closed(db_session, app, tamper):
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    payload = copy.deepcopy(event.payload)
    if tamper == "quote":
        payload["raw"]["event"]["message"]["parent_id"] = "quoted"
    if tamper == "app":
        payload["raw"]["header"]["app_id"] = "wrong"
    if tamper == "chat":
        task.session_key = "wrong"
    if tamper == "bot_sender":
        payload["raw"]["event"]["sender"]["sender_type"] = "app"
    if tamper == "disabled_user":
        user.status = "disabled"
    if tamper == "disabled_bot":
        bot.enabled = False
    if tamper == "task_kind":
        task.kind = "cron_run"
    if tamper == "payload":
        task.payload = {"message": {"parts": [{"type": "text", "text": "两分钟后提醒我偷换"}]}}
    if tamper == "raw_content":
        payload["raw"]["event"]["message"]["content"] = json.dumps({"text": "unrelated"})
    event.payload = payload
    await db_session.commit()
    assert "验证" in await handle_request(db_session, task, app.state.cipher)
    assert await db_session.scalar(select(InteractionState)) is None


async def test_union_fallback_is_supported_without_personal_scope_change(db_session, app):
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    ident = await db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == user.id))
    ident.union_id = "union-human"
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    payload = copy.deepcopy(event.payload)
    payload["sender"].update(platform_user_id="", union_id="union-human")
    payload["raw"]["event"]["sender"]["sender_id"].update(user_id="", union_id="union-human")
    event.sender_platform_user_id = ""
    event.payload = payload
    task.payload = {"message": payload}
    await db_session.commit()
    assert "确认提醒" in await handle_request(db_session, task, app.state.cipher)


async def confirmed(session, app):
    bot, user, task = await direct(session, app, "两分钟后提醒我检查接口")
    await handle_request(session, task, app.state.cipher)
    await set_text(session, task, "确认提醒")
    await handle_request(session, task, app.state.cipher)
    task.status = "succeeded"
    await session.commit()
    return bot, user, await session.scalar(select(CronJob))


async def test_static_scheduler_worker_exact_self_no_model(db_session, app, db_engine):
    from coreman.core.bus import instances, leases, outbox
    from coreman.core.db.models import CronRun
    from coreman.core.db.session import make_session_factory
    from coreman.runtime.gateway_feishu.transport import FeishuTransport
    from coreman.runtime.scheduler.cron import run_tick
    from coreman.runtime.worker.cron_handler import CronRunHandler
    from tests.integration.test_cron_handler import claim
    from tests.integration.test_feishu_transport import FakeAPI
    from tests.integration.worker_helpers import build_ctx

    bot, user, row = await confirmed(db_session, app)
    factory = make_session_factory(db_engine)
    assert await run_tick(factory, row.run_at) == 1
    assert await run_tick(factory, row.run_at) == 0
    await db_session.commit()
    task = await claim(db_session)

    def forbidden(_):
        pytest.fail("Static reminder invoked Relay")

    ctx = build_ctx(db_engine, task, relay_client_factory=forbidden)
    await CronRunHandler().run(ctx)
    await CronRunHandler().run(ctx)
    rows = (await db_session.scalars(select(OutboxItem))).all()
    assert len(rows) == 1 and rows[0].target["chat_id"] == "oc_private"
    assert rows[0].target["recipient_user_id"] == str(user.id)
    assert rows[0].target["recipient_platform_user_id"] == "human"
    assert rows[0].payload["markdown"] == "提醒：检查接口"
    assert (await db_session.scalar(select(CronRun))).status == "success"

    await instances.register(
        db_session,
        instance_id="reminder-delivery-test",
        service="gateway-feishu",
        version="test",
        capacity=None,
    )
    await leases.ensure_rows(db_session, "feishu")
    lease = await leases.acquire(
        db_session,
        bot_id=bot.id,
        platform="feishu",
        instance_id="reminder-delivery-test",
    )
    assert lease is not None
    await db_session.commit()
    api = FakeAPI()
    transport = FeishuTransport(
        factory,
        api,
        bot_id=bot.id,
        instance_id="reminder-delivery-test",
        generation=lease.generation,
    )
    assert await transport.consume_one()
    await db_session.refresh(rows[0])
    assert rows[0].status == "sent"
    sent_calls = len(api.calls)

    changed = await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"cron:{rows[0].id}:identity-changed",
        target=dict(rows[0].target),
        payload={"markdown": "must not send"},
    )
    identity = await db_session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == user.id,
            UserIdentity.platform == "feishu",
        )
    )
    identity.platform_user_id = "changed-human"
    await db_session.commit()
    assert changed is not None and await transport.consume_one()
    await db_session.refresh(changed)
    assert changed.status == "skipped"
    assert changed.last_error == "recipient binding changed"
    assert len(api.calls) == sent_calls


async def test_self_api_owner_only_no_generic_escalation(db_session, app, client):
    from tests.api.conftest import _attach_session, login_as

    bot, user, row = await confirmed(db_session, app)
    await _attach_session(client, db_session, user)
    result = await client.get("/api/self-reminders")
    assert result.status_code == 200 and result.json()["data"][0]["id"] == str(row.id)
    for suffix in ("", "/runs"):
        assert (await client.get(f"/api/admin/cron-jobs/{row.id}{suffix}")).status_code in (
            404,
            405,
        )
    for suffix in ("/force-run", "/test-notification"):
        assert (
            await client.post(f"/api/admin/cron-jobs/{row.id}{suffix}", json={})
        ).status_code in (403, 404, 405)
    assert (
        await client.patch(f"/api/self-reminders/{row.id}", json={"execution_mode": "ai"})
    ).status_code == 405
    await login_as(client, db_session)
    assert (await client.get("/api/self-reminders")).json()["data"] == []
    assert (await client.post(f"/api/self-reminders/{row.id}/cancel")).status_code == 404
    await _attach_session(client, db_session, user)
    assert (await client.post(f"/api/self-reminders/{row.id}/cancel")).status_code == 200
    await db_session.refresh(row)
    assert not row.enabled


@pytest.mark.parametrize("change", ["expired", "past", "wrong_chat", "wrong_human", "wrong_bot"])
async def test_confirmation_scope_and_expiry(db_session, app, change):
    from coreman.core.db.models import User

    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    now = datetime.now(UTC)
    await handle_request(db_session, task, app.state.cipher, now=now)
    await set_text(db_session, task, "确认提醒")
    if change in ("wrong_chat", "wrong_human", "wrong_bot"):
        event = await db_session.get(InboundEvent, task.inbound_event_id)
        payload = copy.deepcopy(event.payload)
        if change == "wrong_chat":
            event.chat_id = task.session_key = "oc_other"
            payload["chat_id"] = "oc_other"
            payload["raw"]["event"]["message"]["chat_id"] = "oc_other"
        elif change == "wrong_human":
            other = User(login_name="other", display_name="other", source="sync")
            db_session.add(other)
            await db_session.flush()
            db_session.add(
                UserIdentity(user_id=other.id, platform="feishu", platform_user_id="other")
            )
            event.sender_platform_user_id = "other"
            payload["sender"]["platform_user_id"] = "other"
            payload["raw"]["event"]["sender"]["sender_id"]["user_id"] = "other"
        else:
            task.bot_id = __import__("uuid").uuid4()
            # Do not persist a nonexistent FK; verified_origin must reject the current mismatch.
        event.payload = payload
        task.payload = {"message": payload}
        if change != "wrong_bot":
            await db_session.flush()
    if change == "wrong_bot":
        with db_session.no_autoflush:
            reply = await handle_request(db_session, task, app.state.cipher, now=now)
        await db_session.rollback()
    else:
        reply = await handle_request(
            db_session,
            task,
            app.state.cipher,
            now=now + timedelta(minutes=6 if change == "expired" else 3 if change == "past" else 0),
        )
    assert "已设置" not in reply
    assert await db_session.scalar(select(CronJob)) is None


@pytest.mark.parametrize(
    "revoked", ["user", "bot", "acl", "reachability", "recipient", "precheck", "mode_snapshot"]
)
async def test_worker_revalidates_static_constraints(db_session, app, db_engine, revoked):
    from coreman.core.db.models import BotAllowedUser, UserReached
    from coreman.core.db.session import make_session_factory
    from coreman.runtime.scheduler.cron import run_tick
    from coreman.runtime.worker.cron_handler import CronRunHandler
    from tests.integration.test_cron_handler import claim
    from tests.integration.worker_helpers import build_ctx

    bot, user, row = await confirmed(db_session, app)
    assert await run_tick(make_session_factory(db_engine), row.run_at) == 1
    await db_session.commit()
    task = await claim(db_session)
    await db_session.refresh(row)
    if revoked == "user":
        user.status = "disabled"
    if revoked == "bot":
        bot.enabled = False
    if revoked == "acl":
        db_session.add(BotAllowedUser(bot_id=bot.id, user_id=bot.created_by))
    if revoked == "reachability":
        (await db_session.get(UserReached, (bot.id, user.id))).platform_chat_id = "oc_wrong"
    if revoked == "recipient":
        row.target_users = [bot.created_by]
    if revoked == "precheck":
        row.precheck_script = 'raise Exception("must not execute")'
    if revoked == "mode_snapshot":
        task.payload = {
            **task.payload,
            "config": {**task.payload["config"], "execution_mode": "ai"},
        }
    await db_session.commit()

    def forbidden(_):
        pytest.fail("No model for revoked reminder")

    await CronRunHandler().run(build_ctx(db_engine, task, relay_client_factory=forbidden))
    assert await db_session.scalar(select(OutboxItem)) is None


async def test_cancel_pending_outbox_preserves_sent_truth(db_session, app, client, db_engine):
    from coreman.core.db.session import make_session_factory
    from coreman.runtime.scheduler.cron import run_tick
    from coreman.runtime.worker.cron_handler import CronRunHandler
    from tests.api.conftest import _attach_session
    from tests.integration.test_cron_handler import claim
    from tests.integration.worker_helpers import build_ctx

    bot, user, row = await confirmed(db_session, app)
    await run_tick(make_session_factory(db_engine), row.run_at)
    await db_session.commit()
    task = await claim(db_session)
    await CronRunHandler().run(build_ctx(db_engine, task))
    await _attach_session(client, db_session, user)
    response = await client.post(f"/api/self-reminders/{row.id}/cancel")
    assert response.status_code == 200 and response.json()["data"]["deliveries"] == ["skipped"]
    item = await db_session.scalar(select(OutboxItem))
    item.status = "sent"
    await db_session.commit()
    response = await client.post(f"/api/self-reminders/{row.id}/cancel")
    assert response.status_code == 200 and response.json()["data"]["deliveries"] == ["sent"]


async def test_stop_command_clears_pending_without_creating(db_session, app):
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    await handle_request(db_session, task, app.state.cipher)
    await set_text(db_session, task, "stop")
    task.kind = "command"
    await db_session.commit()
    assert await handle_request(db_session, task, app.state.cipher) is None
    pending = await db_session.scalar(select(InteractionState))
    assert pending.status == "cancelled"


@pytest.mark.parametrize(
    "text",
    [
        "十十分钟后提醒我检查",
        "两分钟后提醒我和小王检查",
        "两分钟后提醒我们检查",
        "两分钟后提醒我检查，每天一次",
    ],
)
def test_noncanonical_and_multi_recipient_grammar_rejected(text):
    assert parse_request(text) is None


async def test_pending_and_active_limits(db_session, app):
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    for index in range(20):
        db_session.add(
            InteractionState(
                bot_id=bot.id,
                kind="self_reminder",
                scope_key=f"other-{index}",
                state={"user_id": str(user.id)},
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    await db_session.commit()
    assert "20" in await handle_request(db_session, task, app.state.cipher)
    assert len((await db_session.scalars(select(InteractionState))).all()) == 20


async def test_chat_pipeline_intercepts_personal_and_choice_without_relay(
    db_session, app, db_engine
):
    from coreman.core.chat import interactions
    from coreman.core.db.models import FeishuPersonalGrant
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.integration.worker_helpers import build_ctx

    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口，只提醒一次")
    db_session.add(
        FeishuPersonalGrant(
            bot_id=bot.id,
            user_id=user.id,
            app_id="cli_test",
            platform_user_id="human",
            open_id="ou_human",
            tenant_key="tenant-test",
            status="connected",
            assistant_mode="personal",
            token_enc="unused-secret",
        )
    )
    await interactions.open_state(
        db_session,
        bot_id=bot.id,
        kind="choice",
        scope_key=interactions.choice_scope(bot.id, "human"),
        state={"questions": [], "answers": {}, "current_index": 0},
    )
    await db_session.commit()

    def forbidden(_):
        pytest.fail("Reminder must precede personal/choice model paths")

    ctx = build_ctx(db_engine, task, relay_client_factory=forbidden)
    await ChatTaskHandler().run(ctx)
    await db_session.refresh(task)
    assert task.result == {"self_reminder": True}
    assert (
        await db_session.scalar(
            select(InteractionState).where(InteractionState.kind == "self_reminder")
        )
    ).status == "open"
    assert (
        await db_session.scalar(select(InteractionState).where(InteractionState.kind == "choice"))
    ).status == "open"

    # A separate durable incoming message confirms; an outstanding generic choice cannot eat it.
    from tests.integration.test_chat_handler import chat_task

    original = await db_session.get(InboundEvent, task.inbound_event_id)
    confirmation = await chat_task(
        db_session, bot, "确认提醒", sender="human", chat_id="oc_private"
    )
    event = await db_session.get(InboundEvent, confirmation.inbound_event_id)
    event.sender_open_id = "ou_human"
    event.payload = {
        **event.payload,
        "sender": {**event.payload["sender"], "sender_type": "user"},
        "raw": copy.deepcopy(original.payload["raw"]),
    }
    await set_text(db_session, confirmation, "确认提醒")
    await ChatTaskHandler().run(build_ctx(db_engine, confirmation, relay_client_factory=forbidden))
    await db_session.refresh(confirmation)
    assert confirmation.result == {"self_reminder": True}
    job = await db_session.scalar(select(CronJob))
    assert job.created_by == user.id and job.execution_mode == "self_reminder"
    assert job.prompt == "检查接口"
    assert (
        await db_session.scalar(select(InteractionState).where(InteractionState.kind == "choice"))
    ).status == "open"


async def test_all_generic_routes_reject_static_even_bot_admin(db_session, app, client):
    from coreman.core.db.models import BotMember
    from tests.api.conftest import _attach_session

    bot, user, row = await confirmed(db_session, app)
    db_session.add(BotMember(bot_id=bot.id, user_id=user.id))
    await db_session.commit()
    await _attach_session(client, db_session, user)
    for suffix in ("/runs", "/notification-tests"):
        assert (await client.get(f"/api/admin/cron-jobs/{row.id}{suffix}")).status_code == 404
    for suffix in ("/run", "/disable", "/cancel-run", "/test-notification"):
        response = await client.post(
            f"/api/admin/cron-jobs/{row.id}{suffix}",
            json={},
            headers={"If-Match": f'"{row.version}"'},
        )
        assert response.status_code == 404, (suffix, response.text)
    assert (
        await client.delete(
            f"/api/admin/cron-jobs/{row.id}", headers={"If-Match": f'"{row.version}"'}
        )
    ).status_code == 404
    body = {
        "bot_id": str(bot.id),
        "name": "attack",
        "prompt": "attack",
        "cron_expression": "* * * * *",
    }
    assert (
        await client.put(
            f"/api/admin/cron-jobs/{row.id}", json=body, headers={"If-Match": f'"{row.version}"'}
        )
    ).status_code == 404
    assert (await client.get("/api/admin/cron-jobs")).json()["data"]["items"] == []
    for extra in (
        {"execution_mode": "self_reminder"},
        {"created_by": str(user.id)},
        {"reminder_chat_id": "oc_other"},
    ):
        assert (
            await client.post("/api/admin/cron-jobs", json={**body, **extra})
        ).status_code == 422


async def test_concurrent_confirmation_creates_only_one(db_session, app, db_engine):
    import asyncio

    from coreman.core.db.models import Task
    from coreman.core.db.session import make_session_factory

    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    await handle_request(db_session, task, app.state.cipher)
    await set_text(db_session, task, "确认提醒")
    factory = make_session_factory(db_engine)

    async def confirm():
        async with factory() as session:
            current = await session.get(Task, task.id)
            result = await handle_request(session, current, app.state.cipher)
            await session.commit()
            return result

    replies = await asyncio.gather(confirm(), confirm())
    assert sum("已设置" in value for value in replies) == 1
    assert len((await db_session.scalars(select(CronJob))).all()) == 1


@pytest.mark.parametrize("race", [False, True])
async def test_cancel_wins_before_worker_never_enqueues(db_session, app, client, db_engine, race):
    from coreman.core.db.session import make_session_factory
    from coreman.runtime.scheduler.cron import run_tick
    from coreman.runtime.worker.cron_handler import CronRunHandler
    from tests.api.conftest import _attach_session
    from tests.integration.test_cron_handler import claim
    from tests.integration.worker_helpers import build_ctx

    bot, user, row = await confirmed(db_session, app)
    await run_tick(make_session_factory(db_engine), row.run_at)
    await db_session.commit()
    task = await claim(db_session)
    await _attach_session(client, db_session, user)
    if race:
        import asyncio

        response, _ = await asyncio.gather(
            client.post(f"/api/self-reminders/{row.id}/cancel"),
            CronRunHandler().run(build_ctx(db_engine, task)),
        )
        assert response.status_code == 200
        assert all(
            item.status == "skipped"
            for item in (await db_session.scalars(select(OutboxItem))).all()
        )
    else:
        assert (await client.post(f"/api/self-reminders/{row.id}/cancel")).status_code == 200
        await CronRunHandler().run(build_ctx(db_engine, task))
        assert await db_session.scalar(select(OutboxItem)) is None


async def test_active_limit_is_global_for_original_human(db_session, app):
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    await handle_request(db_session, task, app.state.cipher)
    for index in range(20):
        db_session.add(
            CronJob(
                bot_id=bot.id,
                created_by=user.id,
                name=f"fixed-{index}",
                execution_mode="self_reminder",
                reminder_chat_id="oc_private",
                schedule_kind="once",
                run_at=datetime.now(UTC) + timedelta(hours=1),
                cron_expression="",
                prompt="fixed",
                target_users=[user.id],
            )
        )
    await set_text(db_session, task, "确认提醒")
    assert "20" in await handle_request(db_session, task, app.state.cipher)
    assert len((await db_session.scalars(select(CronJob))).all()) == 20


@pytest.mark.parametrize("text", ["一百一小时后提醒我检查", "一百二分钟后提醒我检查"])
def test_ambiguous_shortened_hundreds_require_explicit_digits(text):
    assert parse_request(text) is None


@pytest.mark.parametrize("command", ["stop", "new"])
@pytest.mark.parametrize("tamper", [None, "app", "ambiguous_union", "actor_mismatch"])
async def test_union_stop_uses_current_verified_human(db_session, app, db_engine, command, tamper):
    from coreman.core.db.models import BotAllowedUser, User
    from coreman.runtime.worker.commands import CommandHandler
    from tests.integration.worker_helpers import build_ctx

    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    await handle_request(db_session, task, app.state.cipher)
    await set_text(db_session, task, command)
    ident = await db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == user.id))
    ident.union_id = "union-human"
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    payload = copy.deepcopy(event.payload)
    payload["sender"].update(platform_user_id="", union_id="union-human")
    payload["raw"]["event"]["sender"]["sender_id"].update(user_id="", union_id="union-human")
    if tamper == "app":
        payload["raw"]["header"]["app_id"] = "forged-app"
    if tamper == "ambiguous_union":
        other = User(login_name="ambiguous", display_name="Ambiguous", source="sync")
        db_session.add(other)
        await db_session.flush()
        db_session.add(
            UserIdentity(
                user_id=other.id,
                platform="feishu",
                platform_user_id="other",
                union_id="union-human",
            )
        )
    event.sender_platform_user_id = ""
    event.payload = payload
    task.kind = "command"
    task.payload = {
        "message": payload,
        "command": "reset" if command == "new" else "stop",
        "platform_user_id": "outsider" if tamper == "actor_mismatch" else "",
    }
    db_session.add(BotAllowedUser(bot_id=bot.id, user_id=user.id))
    await db_session.commit()
    await CommandHandler().run(build_ctx(db_engine, task))
    row = await db_session.scalar(
        select(InteractionState).where(InteractionState.kind == "self_reminder")
    )
    await db_session.refresh(row)
    assert row.status == ("open" if tamper else "cancelled")
    await db_session.refresh(task)
    if tamper:
        assert task.result == {"denied": True}


async def set_native_content(session, bot, task, kind, content, *, message_fields=None):
    """Persist the real gateway output, including its whitespace and post normalization."""
    from coreman.runtime.gateway_feishu.inbound import normalize_event

    event = await session.get(InboundEvent, task.inbound_event_id)
    raw = copy.deepcopy(event.payload["raw"])
    raw["event"]["message"].update(message_type=kind, content=json.dumps(content))
    raw["event"]["message"].update(message_fields or {})
    normalized = normalize_event(
        raw,
        bot_id=bot.id,
        app_id="cli_test",
        bot_open_id="ou_bot",
        gateway_instance="gw",
        now=datetime.now(UTC),
    )
    assert normalized is not None
    event.payload = normalized.model_dump(mode="json")
    event.reply_context = normalized.reply_context
    task.payload = {"message": event.payload}
    await session.commit()


@pytest.mark.parametrize("shape", ["direct", "localized", "v2", "segments"])
async def test_native_plain_post_proposal_and_confirmation(db_session, app, shape):
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口，只提醒一次")

    def post(text):
        segments = [{"tag": "text", "text": text}]
        if shape == "segments":
            segments = [{"tag": "text", "text": text[:2]}, {"tag": "text", "text": text[2:]}]
        result = {"title": "", "content_v2" if shape == "v2" else "content": [segments]}
        return {"zh_cn": result} if shape == "localized" else result

    await set_native_content(
        db_session, bot, task, "post", post("两分钟后提醒我检查接口，只提醒一次")
    )
    assert "确认提醒" in await handle_request(db_session, task, app.state.cipher)
    assert await db_session.scalar(select(CronJob)) is None
    await set_native_content(db_session, bot, task, "post", post("确认提醒"))
    assert "已设置" in await handle_request(db_session, task, app.state.cipher)
    job = await db_session.scalar(select(CronJob))
    assert job.created_by == user.id and job.prompt == "检查接口"


async def test_native_text_whitespace_uses_gateway_strip(db_session, app):
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    await set_native_content(db_session, bot, task, "text", {"text": "  两分钟后提醒我检查接口  "})
    assert "确认提醒" in await handle_request(db_session, task, app.state.cipher)
    await set_native_content(db_session, bot, task, "text", {"text": "\n确认提醒\n"})
    assert "已设置" in await handle_request(db_session, task, app.state.cipher)
    assert (await db_session.scalar(select(CronJob))).prompt == "检查接口"


@pytest.mark.parametrize(
    "tamper",
    [
        "image",
        "link",
        "markdown",
        "code",
        "unknown",
        "title",
        "multiline",
        "locales",
        "both_content",
        "quoted",
        "forward",
        "normalized_mismatch",
    ],
)
async def test_native_post_rejects_rich_ambiguous_and_quoted_commands(db_session, app, tamper):
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    text = "两分钟后提醒我检查接口"
    content = {"title": "", "content": [[{"tag": "text", "text": text}]]}
    fields = {}
    if tamper in ("image", "link", "markdown", "code", "unknown"):
        content["content"][0].append(
            {
                "tag": {
                    "image": "img",
                    "link": "a",
                    "markdown": "md",
                    "code": "code_block",
                    "unknown": "mystery",
                }[tamper],
                "text": "",
                "image_key": "img_test",
            }
        )
    if tamper == "title":
        content["title"] = "other request"
    if tamper == "multiline":
        content["content"].append([{"tag": "text", "text": "another line"}])
    if tamper == "locales":
        content = {"zh_cn": content, "en_us": copy.deepcopy(content)}
    if tamper == "both_content":
        content["content_v2"] = copy.deepcopy(content["content"])
    if tamper == "quoted":
        fields["parent_id"] = "quoted-message"
    if tamper == "forward":
        content["content"][0].append({"tag": "message", "message_id": "forwarded"})
    await set_native_content(db_session, bot, task, "post", content, message_fields=fields)
    if tamper == "normalized_mismatch":
        event = await db_session.get(InboundEvent, task.inbound_event_id)
        event.payload = {**event.payload, "parts": [{"type": "text", "text": "两分钟后提醒我伪造"}]}
        task.payload = {"message": event.payload}
        await db_session.commit()
    assert "验证" in await handle_request(db_session, task, app.state.cipher)
    assert await db_session.scalar(select(InteractionState)) is None


@pytest.mark.parametrize("confirmation", ["mixed", "quoted", "extra_words"])
async def test_native_post_confirmation_keeps_exact_plain_command_boundary(
    db_session, app, confirmation
):
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口")
    content = {"content": [[{"tag": "text", "text": "两分钟后提醒我检查接口"}]]}
    await set_native_content(db_session, bot, task, "post", content)
    assert "确认提醒" in await handle_request(db_session, task, app.state.cipher)
    content = {
        "content": [
            [
                {
                    "tag": "text",
                    "text": "确认提醒谢谢" if confirmation == "extra_words" else "确认提醒",
                }
            ]
        ]
    }
    if confirmation == "mixed":
        content["content"][0].append({"tag": "message", "message_id": "forward"})
    await set_native_content(
        db_session,
        bot,
        task,
        "post",
        content,
        message_fields={"parent_id": "quoted"} if confirmation == "quoted" else None,
    )
    reply = await handle_request(db_session, task, app.state.cipher)
    if confirmation == "extra_words":
        assert reply is None  # Extra prose belongs to the assistant, never confirmation.
    else:
        assert reply is not None and "已设置" not in reply
    assert await db_session.scalar(select(CronJob)) is None
    assert (await db_session.scalar(select(InteractionState))).status == "open"


@pytest.mark.parametrize("text", ["确认提醒这四个字翻译成英文", "取消提醒用英语怎么说？"])
@pytest.mark.parametrize("mode", ["single", "group", "personal"])
async def test_confirmation_words_reach_assistant(db_session, app, db_engine, text, mode):
    from tests.api.test_feishu_personal import grant
    from tests.api.test_feishu_personal_worker import intake_for
    from tests.fakes.fake_relay import FakeRelay
    from tests.integration.test_chat_handler import run

    bot, user, task = await setup(
        db_session, app, chat_type="group" if mode == "group" else "single"
    )
    await set_text(db_session, task, text)
    if mode == "personal":
        await grant(db_session, app, bot, user)
        await intake_for(db_session, bot, task, text)
    await db_session.commit()
    relay = FakeRelay("normal")
    await run(db_engine, task, relay)
    assert len(relay.requests) == 1
    assert text in str(relay.requests[0]["messages"])
    assert ("COREMAN_FEISHU_PERSONAL_TOKEN" in relay.requests[0]["env_vars"]) == (
        mode == "personal"
    )
    assert await db_session.scalar(select(CronJob)) is None
    assert await db_session.scalar(select(InteractionState)) is None
