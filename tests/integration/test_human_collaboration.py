"""Human colleague help: discovery, reach decision, real reply binding, resume and expiry."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from coreman.core.bus import instances, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import human_collaboration as human
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BotCollaborationPartner,
    BotHumanPartner,
    CronJob,
    CronRun,
    HumanCollaboration,
    InboundEvent,
    OutboxItem,
    Task,
    TaskStream,
    User,
    UserIdentity,
)
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.worker.chat.models import Verdict
from tests.fakes.fake_relay import FakeRelay
from tests.integration.test_chat_handler import chat_task, run
from tests.integration.worker_helpers import MASTER, build_ctx, seed_bot


async def setup(session, *, chat_type="group"):
    bot, relay, _ = await seed_bot(session)
    bot.platform = "feishu"
    origin = User(login_name="asker", display_name="提问人")
    helper = User(login_name="expert", display_name="库存专家", position="库存主管")
    session.add_all([origin, helper])
    await session.flush()
    session.add_all(
        [
            UserIdentity(user_id=origin.id, platform="feishu", platform_user_id="asker-id"),
            UserIdentity(user_id=helper.id, platform="feishu", platform_user_id="expert-id"),
        ]
    )
    partner = BotHumanPartner(
        source_bot_id=bot.id, user_id=helper.id, responsibility="负责库存口径"
    )
    session.add(partner)
    await session.commit()
    chat_id = "group" if chat_type == "group" else "p2p-asker"
    task = await chat_task(
        session, bot, "库存口径是什么", sender="asker-id", chat_type=chat_type, chat_id=chat_id
    )
    return bot, origin, helper, partner, task


async def call(session, task, actor, name, args, *, member=True):
    from coreman.core.chat.collaboration_tools import invoke

    with patch.object(human, "in_group", new=AsyncMock(return_value=member)):
        result = await invoke(
            session,
            task_id=task.id,
            actor=str(actor.id),
            name=name,
            arguments=args,
            cipher=Cipher(MASTER),
        )
    await session.commit()
    return result


async def register(session, task, origin, helper, *, member=True):
    result = await call(
        session,
        task,
        origin,
        "request_collaboration",
        {"collaborator_id": f"human:{helper.id}", "question": "A 仓的可用库存口径是什么？"},
        member=member,
    )
    assert result["stop"] is True, result
    return await session.scalar(
        select(HumanCollaboration).where(HumanCollaboration.source_task_id == task.id)
    )


async def handoff(session, engine, task, row, *, chat_type="group"):
    from coreman.runtime.worker.chat.human_collaboration import final_transition

    ctx = build_ctx(engine, task)
    pre = SimpleNamespace(intake=SimpleNamespace(chat_type=chat_type))
    verdict = await final_transition(
        session, ctx, pre, Verdict("success", "succeeded", None, None, "")
    )
    await session.flush()
    # The gateway records the delivered ask id; only replies quoting it count.
    item = await session.get(OutboxItem, row.request_outbox_id)
    item.status = "sent"
    row.request_message_id = "om_ask"
    await session.commit()
    return verdict, item


async def inbound_task(session, bot, text, *, sender, chat_type, chat_id, parent=None, parts=None):
    mid = f"m-{uuid.uuid4()}"
    payload = {
        "platform": "feishu",
        "kind": "message",
        "chat_type": chat_type,
        "chat_id": chat_id,
        "sender": {"platform_user_id": sender, "open_id": None},
        "message_id": mid,
        "mentions_bot": True,
        "parts": parts if parts is not None else [{"type": "text", "text": text}],
        "card_action": None,
        "raw": {},
    }
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=mid,
        kind="message",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_platform_user_id=sender,
        payload=payload,
        reply_context={"chat_id": chat_id, "message_id": mid, "parent_id": parent},
    )
    session.add(event)
    await session.flush()
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="chat",
            payload={"message": payload, "bot_key": bot.bot_key, "platform_user_id": sender},
            session_key=chat_id,
            inbound_event_id=event.id,
        ),
    )
    await session.commit()
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    claimed = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert claimed and claimed.id == task.id
    return claimed


async def final_text(session, task_id):
    stream = await session.scalar(select(TaskStream).where(TaskStream.task_id == task_id))
    return stream.final_text if stream else None


async def test_discover_detail_and_register_member_in_group(db_session):
    bot, origin, helper, partner, task = await setup(db_session)
    page = await call(db_session, task, origin, "search_collaborators", {"query": "库存"})
    assert page["items"] == [
        {
            "id": f"human:{helper.id}",
            "type": "human",
            "name": "库存专家",
            "summary": "负责库存口径；库存主管",
        }
    ]
    detail = await call(db_session, task, origin, "get_collaborator", {"id": f"human:{helper.id}"})
    assert detail["responsibility"] == "负责库存口径" and detail["type"] == "human"
    row = await register(db_session, task, origin, helper)
    assert (row.status, row.channel, row.origin_chat_id) == ("pending", "group", "group")
    assert row.origin_event_id == task.inbound_event_id
    assert row.helper_platform_user_id == "expert-id"
    await db_session.refresh(task)
    assert task.payload["collaboration_handoff"] is True
    # Registration notifies nobody: the ask is queued only after a clean handoff.
    assert not await db_session.scalar(select(OutboxItem.id).limit(1))


async def test_not_a_member_or_private_chat_uses_direct_message(db_session):
    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper, member=False)
    assert row.channel == "direct"


async def test_private_chat_lists_colleagues_but_never_other_employees(db_session):
    bot, origin, helper, partner, task = await setup(db_session, chat_type="single")
    peer = Bot(
        bot_key="peer",
        platform="feishu",
        name="库存机器人",
        description="库存",
        created_by=bot.created_by,
        relay_server_id=bot.relay_server_id,
        model=bot.model,
        working_dir="/peer",
        credentials_enc=bot.credentials_enc,
        env_vars_enc=bot.env_vars_enc,
    )
    db_session.add(peer)
    await db_session.flush()
    db_session.add(BotCollaborationPartner(source_bot_id=bot.id, target_bot_id=peer.id))
    await db_session.commit()
    page = await call(db_session, task, origin, "search_collaborators", {})
    assert [item["type"] for item in page["items"]] == ["human"]
    denied = await call(db_session, task, origin, "get_collaborator", {"id": "peer"})
    assert denied["error"] == "peer unavailable or unauthorized"
    row = await register(db_session, task, origin, helper)
    assert (row.channel, row.origin_chat_type) == ("direct", "single")


async def test_originator_is_never_offered_or_asked(db_session):
    bot, origin, helper, partner, task = await setup(db_session)
    db_session.add(BotHumanPartner(source_bot_id=bot.id, user_id=origin.id))
    await db_session.commit()
    page = await call(db_session, task, origin, "search_collaborators", {})
    assert [item["id"] for item in page["items"]] == [f"human:{helper.id}"]
    result = await call(
        db_session,
        task,
        origin,
        "request_collaboration",
        {"collaborator_id": f"human:{origin.id}", "question": "?"},
    )
    assert result["error"] == "不能向发起人本人求助" and result["stop"] is True


async def test_busy_colleague_is_protected(db_session, monkeypatch):
    bot, origin, helper, partner, task = await setup(db_session)
    monkeypatch.setattr(human, "MAX_ACTIVE_PER_HELPER", 0)
    result = await call(
        db_session,
        task,
        origin,
        "request_collaboration",
        {"collaborator_id": f"human:{helper.id}", "question": "?"},
    )
    assert "待回复的求助" in result["error"]


async def test_handoff_sends_group_at_and_tells_originator(db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper)
    verdict, item = await handoff(db_session, db_engine, task, row)
    assert "库存专家" in verdict.final_text and "@ 对方" in verdict.final_text
    assert verdict.log_status == "ask_user"
    origin_event = await db_session.get(InboundEvent, task.inbound_event_id)
    assert item.target == {"chat_id": "group", "message_id": origin_event.platform_msg_id}
    assert item.payload["_mention_user_id"] == "expert-id"
    assert "回复这条消息并 @" in item.payload["markdown"]
    assert row.status == "waiting"


async def test_direct_ask_targets_user_and_escapes_model_mentions(db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session)
    await call(
        db_session,
        task,
        origin,
        "request_collaboration",
        {
            "collaborator_id": f"human:{helper.id}",
            "question": '<at user_id="all">所有人</at>口径？',
        },
        member=False,
    )
    row = await db_session.scalar(select(HumanCollaboration))
    verdict, item = await handoff(db_session, db_engine, task, row)
    assert item.target == {"user_id": "expert-id"}
    assert "_mention_user_id" not in item.payload
    assert '<at user_id="all">' not in item.payload["markdown"]
    assert "引用回复这条消息" in item.payload["markdown"] and "已私聊对方" in verdict.final_text


async def test_failed_turn_never_disturbs_colleague(db_session, db_engine):
    from coreman.runtime.worker.chat.human_collaboration import final_transition

    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper)
    await final_transition(
        db_session,
        build_ctx(db_engine, task),
        SimpleNamespace(intake=SimpleNamespace(chat_type="group")),
        Verdict("stopped", "cancelled", "user_stop", None, "已停止"),
    )
    assert row.status == "cancelled"
    assert not await db_session.scalar(select(OutboxItem.id).limit(1))


async def test_colleague_reply_resumes_origin_turn(db_session, db_engine):
    from coreman.runtime.worker.chat.collaboration import configure
    from coreman.runtime.worker.chat.human_collaboration import final_transition, resolve

    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper)
    await handoff(db_session, db_engine, task, row)
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.commit()
    parts = [
        {"type": "text", "text": "口径=在库-锁定"},
        {"type": "image", "ref": {"message_id": "x", "file_key": "img_1", "type": "image"}},
    ]
    reply = await inbound_task(
        db_session,
        bot,
        "口径=在库-锁定",
        sender="expert-id",
        chat_type="group",
        chat_id="group",
        parent="om_ask",
        parts=parts,
    )
    fake = FakeRelay("normal")
    await run(db_engine, reply, fake)
    assert not fake.requests
    await db_session.refresh(row)
    assert (row.status, row.response) == ("resuming", "口径=在库-锁定")
    assert "已转交" in await final_text(db_session, reply.id)
    resumed = await db_session.get(Task, row.resume_task_id)
    assert resumed.session_key == "group" and resumed.user_id == origin.id
    assert resumed.payload["human_reply_message_id"] == reply.payload["message"]["message_id"]
    ctx = build_ctx(db_engine, resumed)
    intake, relay, content = await resolve(db_session, ctx)
    data = json.loads(intake.text)
    assert data["colleague_reply"] == "口径=在库-锁定" and data["colleague"] == "库存专家"
    assert data["colleague_attachments"] == 1 and content[1]["type"] == "image"
    assert intake.inbound.id == task.inbound_event_id and intake.speaker.user_id == origin.id
    info = SimpleNamespace(relay_session_id=uuid.uuid4())
    prompt, env = await configure(db_session, ctx, intake, info, "system", {})
    assert "不得调用协作工具" in prompt and "COREMAN_COLLABORATION_TOKEN" not in env
    await final_transition(
        db_session, ctx, SimpleNamespace(), Verdict("success", "succeeded", None, None, "ok")
    )
    assert row.status == "completed"


async def test_other_people_quoting_the_ask_stay_ordinary_chat(db_session, db_engine):
    from coreman.runtime.worker.chat.human_collaboration import consume_reply
    from coreman.runtime.worker.chat.intake import IntakeStage

    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper)
    await handoff(db_session, db_engine, task, row)
    other = await inbound_task(
        db_session,
        bot,
        "我也想知道",
        sender="asker-id",
        chat_type="group",
        chat_id="group",
        parent="om_ask",
    )
    ctx = build_ctx(db_engine, other)
    inbound = await db_session.get(InboundEvent, other.inbound_event_id)
    speaker = SimpleNamespace(user_id=origin.id, platform_user_id="asker-id")
    assert not await consume_reply(db_session, ctx, bot, inbound, speaker, [], "我也想知道")
    assert row.status == "waiting"
    assert IntakeStage  # the normal pipeline handles it


async def test_late_reply_after_close_is_acknowledged_not_resumed(db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper, member=False)
    await handoff(db_session, db_engine, task, row)
    await human.close(db_session, row, "timed_out", "超时")
    await db_session.commit()
    late = await inbound_task(
        db_session,
        bot,
        "晚了",
        sender="expert-id",
        chat_type="single",
        chat_id="p2p-expert",
        parent="om_ask",
    )
    await run(db_engine, late, FakeRelay("normal"))
    await db_session.refresh(row)
    assert row.status == "timed_out" and row.resume_task_id is None
    assert "已结束" in await final_text(db_session, late.id)


async def test_tick_reminds_then_times_out_and_reports_delivery_failure(db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper)
    await handoff(db_session, db_engine, task, row)
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.commit()
    now = datetime.now(UTC)
    await human.tick(db_session, now + timedelta(hours=3))
    assert row.reminded_at is not None
    remind = await db_session.scalar(
        select(OutboxItem).where(OutboxItem.dedupe_key == f"human-collaboration:{row.id}:remind")
    )
    assert remind.target["message_id"] == "om_ask" and remind.payload["_mention_user_id"]
    await human.tick(db_session, now + timedelta(hours=25))
    assert row.status == "timed_out"
    notices = {
        i.dedupe_key.rsplit(":", 1)[-1]
        for i in await db_session.scalars(
            select(OutboxItem).where(OutboxItem.dedupe_key.like(f"human-collaboration:{row.id}:%"))
        )
    }
    assert {"ask", "remind", "closed", "timed_out"} <= notices
    # Delivery failure is reported to the originator instead of waiting 24 hours.
    task2 = await chat_task(
        db_session, bot, "再问", sender="asker-id", chat_type="group", chat_id="group"
    )
    row2 = await register(db_session, task2, origin, helper, member=False)
    _, item = await handoff(db_session, db_engine, task2, row2)
    item.status, item.last_error = "failed", "Feishu 230013: Bot has NO availability"
    await db_session.commit()
    await human.tick(db_session, datetime.now(UTC))
    assert row2.status == "failed" and "可用范围" in row2.error


async def test_revoked_partner_cancels_waiting_ask(db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper)
    await handoff(db_session, db_engine, task, row)
    partner.enabled = False
    await db_session.commit()
    await human.tick(db_session, datetime.now(UTC))
    assert row.status == "cancelled" and "协作名单" in row.error


async def test_pausing_colleague_after_answer_keeps_the_continuation(db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper)
    await handoff(db_session, db_engine, task, row)
    await tasks.finish(db_session, task.id, status="succeeded")
    event = await db_session.get(InboundEvent, task.inbound_event_id)
    await human.record_reply(db_session, row, event=event, text="口径=在库-锁定")
    partner.enabled = False
    await db_session.commit()
    await human.cancel_partner(db_session, partner, "管理员已暂停")
    await human.tick(db_session, datetime.now(UTC))
    assert row.status == "resuming"


async def test_colleague_only_turns_are_not_tool_capped(db_session, db_engine, monkeypatch):
    from tests.fakes.fake_relay import DONE, FINISH, SCENARIOS, _tool

    bot, origin, helper, partner, task = await setup(db_session, chat_type="single")
    monkeypatch.setitem(
        SCENARIOS, "busy", lambda: [_tool("Bash", f"t{n}") for n in range(80)] + [FINISH, DONE]
    )
    fake = FakeRelay("busy")
    await run(db_engine, task, fake)
    await db_session.refresh(task)
    # The collaboration tools are mounted, but a long ordinary turn is not cut off at 64 tools.
    assert fake.requests[0]["env_vars"]["COREMAN_COLLABORATION_TOKEN"]
    assert task.status == "succeeded" and fake.aborted == 0


async def test_stop_withdraws_only_own_asks(db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper)
    await handoff(db_session, db_engine, task, row)
    assert await human.stop_for(db_session, bot.id, "group", "someone-else") == 0
    assert row.status == "waiting"
    assert await human.stop_for(db_session, bot.id, "group", "asker-id") == 1
    assert row.status == "cancelled"


async def test_cancel_word_from_originator_routes_to_stop(db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session, chat_type="single")
    row = await register(db_session, task, origin, helper)
    await handoff(db_session, db_engine, task, row, chat_type="single")
    msg = InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="message",
        chat_type="single",
        chat_id="p2p-asker",
        sender={"platform_user_id": "asker-id"},
        message_id="m-cancel",
        parts=[{"type": "text", "text": "取消"}],
        reply_context={"chat_id": "p2p-asker", "message_id": "m-cancel"},
    )
    created = await enqueue_inbound(
        db_session,
        SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None),
        msg,
        lease_generation=1,
    )
    assert created.kind == "command" and created.payload["command"] == "stop"


async def test_gateway_does_not_let_the_answer_supersede_group_work(db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper)
    await handoff(db_session, db_engine, task, row)
    with patch(
        "coreman.core.chat.bot_collaboration.admit_human", new=AsyncMock(return_value=None)
    ) as admit:
        for mid, parent in (("m-answer", "om_ask"), ("m-new", None)):
            msg = InboundMessage(
                platform="feishu",
                bot_id=bot.id,
                kind="message",
                chat_type="group",
                chat_id="group",
                sender={"platform_user_id": "expert-id"},
                message_id=mid,
                mentions_bot=True,
                parts=[{"type": "text", "text": "答复"}],
                reply_context={"chat_id": "group", "message_id": mid, "parent_id": parent},
            )
            await enqueue_inbound(
                db_session,
                SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None),
                msg,
                lease_generation=1,
            )
    # Only the unrelated message is treated as a new group request.
    assert admit.await_count == 1


async def test_resume_runs_through_the_worker_in_the_current_group_session(db_session, db_engine):
    bot, origin, helper, partner, task = await setup(db_session)
    row = await register(db_session, task, origin, helper)
    await handoff(db_session, db_engine, task, row)
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.commit()
    reply = await inbound_task(
        db_session,
        bot,
        "口径=在库-锁定",
        sender="expert-id",
        chat_type="group",
        chat_id="group",
        parent="om_ask",
    )
    await run(db_engine, reply, FakeRelay("normal"))
    await db_session.refresh(row)
    resumed = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
    await db_session.commit()
    assert resumed is not None and resumed.id == row.resume_task_id
    fake = FakeRelay("normal")
    await run(db_engine, resumed, fake)
    await db_session.refresh(row)
    assert row.status == "completed"
    body = fake.requests[0]
    assert "口径=在库-锁定" in json.dumps(body["messages"], ensure_ascii=False)
    assert "COREMAN_COLLABORATION_TOKEN" not in body.get("env_vars", {})
    # The final answer replies to the original question, not to the colleague's message.
    stream = await db_session.scalar(select(TaskStream).where(TaskStream.task_id == resumed.id))
    origin_event = await db_session.get(InboundEvent, task.inbound_event_id)
    assert stream.reply_context == origin_event.reply_context


async def test_cron_run_hands_off_then_follow_up_run_delivers_result(db_session, db_engine):
    import asyncio

    from coreman.core.chat.collaboration_tools import invoke
    from coreman.core.db.session import make_session_factory
    from coreman.runtime.scheduler.cron import run_tick
    from coreman.runtime.worker.cron_handler import CronRunHandler
    from tests.integration.test_cron_handler import claim
    from tests.integration.test_cron_scheduler import job

    now = datetime.now(UTC)
    row_job = await job(db_session, now, target_chats=["test-group"])
    bot = await db_session.get(Bot, row_job.bot_id)
    bot.platform = "feishu"
    creator = await db_session.get(User, row_job.created_by)
    helper = User(login_name="expert", display_name="库存专家")
    db_session.add(helper)
    await db_session.flush()
    db_session.add_all(
        [
            UserIdentity(user_id=creator.id, platform="feishu", platform_user_id="creator-id"),
            UserIdentity(user_id=helper.id, platform="feishu", platform_user_id="expert-id"),
            BotHumanPartner(source_bot_id=bot.id, user_id=helper.id, responsibility="库存"),
        ]
    )
    await db_session.commit()
    await run_tick(make_session_factory(db_engine), now)
    task = await claim(db_session)
    slow = FakeRelay("normal", first_byte_delay=5)
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _: slow.client())
    running = asyncio.create_task(CronRunHandler().run(ctx))
    try:
        for _ in range(200):
            if slow.requests:
                break
            await asyncio.sleep(0.02)
        env = slow.requests[0]["env_vars"]
        assert env["COREMAN_COLLABORATION_URL"].endswith("/api/runtime/collaboration/mcp")
        assert "定时执行" in slow.requests[0]["messages"][0]["content"]
        result = await invoke(
            db_session,
            task_id=task.id,
            actor=str(creator.id),
            name="request_collaboration",
            arguments={"collaborator_id": f"human:{helper.id}", "question": "差异原因？"},
        )
        await db_session.commit()
        assert result["channel"] == "direct"
        await asyncio.wait_for(running, 4)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert slow.aborted == 1
    first = await db_session.scalar(select(CronRun).where(CronRun.task_id == task.id))
    assert first.status == "success" and "库存专家" in first.reply
    row = await db_session.scalar(select(HumanCollaboration))
    assert row.status == "waiting" and str(row.relay_session_id) == slow.requests[0]["session_id"]
    ask = await db_session.get(OutboxItem, row.request_outbox_id)
    assert ask.target == {"user_id": "expert-id"} and "定时任务「日报」" in ask.payload["markdown"]
    reply = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id="m-reply",
        kind="message",
        chat_type="single",
        chat_id="p2p-expert",
        sender_platform_user_id="expert-id",
        payload={"parts": []},
        reply_context={},
    )
    db_session.add(reply)
    await db_session.flush()
    await human.record_reply(db_session, row, event=reply, text="盘点漏扫 3 箱")
    await db_session.commit()
    follow = await claim(db_session)
    assert follow.id == row.resume_task_id
    fake = FakeRelay("normal")
    await CronRunHandler().run(
        build_ctx(db_engine, follow, relay_client_factory=lambda _: fake.client())
    )
    body = fake.requests[0]
    assert body["session_id"] == str(row.relay_session_id)
    assert "COREMAN_COLLABORATION_TOKEN" not in body.get("env_vars", {})
    assert "定时任务的续跑" in body["messages"][0]["content"]
    assert "盘点漏扫 3 箱" in json.dumps(body["messages"], ensure_ascii=False)
    second = await db_session.scalar(select(CronRun).where(CronRun.task_id == follow.id))
    await db_session.refresh(row)
    await db_session.refresh(row_job)
    assert second.status == "success" and second.trigger_kind == "collaboration"
    assert row.status == "completed" and row_job.running_task_id is None
    pushed = await db_session.scalars(
        select(OutboxItem).where(OutboxItem.target["chat_id"].astext == "test-group")
    )
    assert len(pushed.all()) == 2


@pytest.mark.parametrize("busy", [False, True])
async def test_cron_ask_resumes_as_collaboration_run(db_session, busy):
    bot, origin, helper, partner, _ = await setup(db_session)
    job = CronJob(
        bot_id=bot.id,
        name="每日库存",
        cron_expression="0 9 * * *",
        prompt="汇总库存异常",
        created_by=origin.id,
    )
    db_session.add(job)
    await db_session.flush()
    source = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind="cron_run",
            user_id=origin.id,
            payload={"cron_job_id": str(job.id), "config": {"name": "每日库存"}},
        ),
    )
    db_session.add(
        CronRun(
            cron_job_id=job.id,
            bot_id=bot.id,
            job_name=job.name,
            task_id=source.id,
            executed_by=origin.id,
            status="success",
            prompt="汇总库存异常",
            started_at=datetime.now(UTC),
            private=True,
        )
    )
    await db_session.commit()
    row = await human.request_help(
        db_session,
        task=source,
        source=None,
        actor=origin.id,
        target_key=f"human:{helper.id}",
        question="昨天 A 仓盘点差异原因？",
        cipher=None,
    )
    assert (row.origin_kind, row.channel, row.cron_job_id) == ("cron", "direct", job.id)
    with pytest.raises(ValueError, match="上一次的求助"):
        await human.request_help(
            db_session,
            task=await tasks.enqueue(
                db_session,
                NewTask(
                    bot_id=bot.id,
                    kind="cron_run",
                    user_id=origin.id,
                    payload={"cron_job_id": str(job.id), "config": {}},
                ),
            ),
            source=None,
            actor=origin.id,
            target_key=f"human:{helper.id}",
            question="?",
            cipher=None,
        )
    await human.send_ask(db_session, row)
    job.running_task_id = 999_999 if busy else None
    reply = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id="m-reply",
        kind="message",
        chat_type="single",
        chat_id="p2p-expert",
        sender_platform_user_id="expert-id",
        payload={"parts": []},
        reply_context={},
    )
    db_session.add(reply)
    await db_session.flush()
    await human.record_reply(db_session, row, event=reply, text="盘点漏扫 3 箱")
    if busy:
        assert row.status == "answered"
        job.running_task_id = None
        await human.tick(db_session, datetime.now(UTC))
    assert row.status == "resuming"
    follow = await db_session.get(Task, row.resume_task_id)
    assert follow.kind == "cron_run" and follow.payload["human_collaboration_id"] == str(row.id)
    assert job.running_task_id == follow.id
    run = await db_session.scalar(select(CronRun).where(CronRun.task_id == follow.id))
    assert run.trigger_kind == "collaboration" and run.private is True
    prompt = json.loads(run.prompt)
    assert prompt["original_task"] == "汇总库存异常"
    assert prompt["colleague_reply"] == "盘点漏扫 3 箱"
