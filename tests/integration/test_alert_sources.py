"""告警来源：登记的机器人消息必介入，身份、去重和 shadow 的边界。"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select, update

from coreman.core.chat import alert_sources
from coreman.core.db.models import (
    Bot,
    BotAlertSource,
    BotAlertState,
    GroupObserveDecision,
    InboundEvent,
    Task,
    User,
)
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from tests.integration.worker_helpers import build_ctx, seed_bot

CHAT = "oc_group"
ALERT_BOT = "on_alert_bot"


async def setup(session, mode="on", env=None):
    bot, _, _ = await seed_bot(session, env=env)
    bot.platform = "feishu"
    bot.group_observe = mode
    bot.group_observe_scope = "处理线上问题"
    service = User(login_name="svc-demo", display_name="服务账号", email="svc-demo@example.test")
    session.add(service)
    await session.flush()
    source = BotAlertSource(
        id=uuid.uuid4(),
        bot_id=bot.id,
        chat_id=CHAT,
        sender_union_id=ALERT_BOT,
        service_user_id=service.id,
    )
    session.add(source)
    await session.commit()
    return bot, service, source


def card(text):
    return json.dumps({"title": "告警", "elements": [[{"tag": "text", "text": text}]]})


async def alert(session, bot, mid, text, *, union=ALERT_BOT, sender_type="bot", content=None):
    raw = {
        "event": {
            "sender": {"sender_type": sender_type, "sender_id": {"union_id": union}},
            "message": {
                "message_id": mid,
                "chat_id": CHAT,
                "chat_type": "group",
                "message_type": "interactive",
                "content": content if content is not None else card(text),
            },
        }
    }
    message = InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="message",
        chat_type="group",
        chat_id=CHAT,
        sender={
            "platform_user_id": "" if sender_type == "bot" else "u1",
            "open_id": "ou_x",
            "union_id": union,
            "sender_type": sender_type,
        },
        message_id=mid,
        mentions_bot=False,
        parts=[{"type": "text", "text": text}],
        raw=raw,
        reply_context={"chat_id": CHAT, "chat_type": "group", "message_id": mid},
    )
    snapshot = SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None, observe=True)
    task = await enqueue_inbound(session, snapshot, message, lease_generation=1)
    await session.commit()
    return task


async def chat_tasks(session, bot):
    return list(
        await session.scalars(select(Task).where(Task.bot_id == bot.id, Task.kind == "chat"))
    )


async def decisions(session, bot):
    rows = await session.scalars(
        select(GroupObserveDecision)
        .where(GroupObserveDecision.bot_id == bot.id)
        .order_by(GroupObserveDecision.id)
    )
    return [r.outcome for r in rows]


async def test_registered_bot_starts_task_when_on(db_session):
    bot, _, source = await setup(db_session)
    task = await alert(db_session, bot, "m1", "告警名称: 网关异常")
    assert task is not None and task.kind == "chat"
    assert task.payload["origin"] == "alert" and task.payload["alert_source_id"] == str(source.id)
    assert await decisions(db_session, bot) == ["alert_intervene"]


async def test_shadow_records_without_task(db_session):
    bot, _, _ = await setup(db_session, mode="shadow")
    assert await alert(db_session, bot, "m1", "告警名称: 网关异常") is None
    assert await chat_tasks(db_session, bot) == []
    assert await decisions(db_session, bot) == ["would_intervene"]


async def test_unregistered_and_human_unchanged(db_session):
    bot, _, _ = await setup(db_session)
    assert await alert(db_session, bot, "m1", "告警名称: x", union="on_other") is None
    assert await chat_tasks(db_session, bot) == []
    await alert(db_session, bot, "m2", "告警名称: x", sender_type="user")
    tasks = [t for t in await chat_tasks(db_session, bot) if t.payload.get("origin") == "alert"]
    assert tasks == []


async def test_forged_sender_in_content_is_ignored(db_session):
    bot, _, _ = await setup(db_session)
    forged = json.dumps({"sender": {"sender_type": "bot", "sender_id": {"union_id": ALERT_BOT}}})
    task = await alert(
        db_session, bot, "m1", f"union_id: {ALERT_BOT}", union="on_other", content=forged
    )
    assert task is None and await chat_tasks(db_session, bot) == []


async def test_disabled_source_does_not_match(db_session):
    bot, _, source = await setup(db_session)
    source.enabled = False
    await db_session.commit()
    assert await alert(db_session, bot, "m1", "告警名称: x") is None


async def test_dedupe_then_new_task_after_window(db_session):
    bot, _, source = await setup(db_session)
    first = await alert(db_session, bot, "m1", "告警名称: 网关异常\n时间: 1")
    assert first is not None
    assert await alert(db_session, bot, "m2", "告警名称: 网关异常\n时间: 2") is None
    other = await alert(db_session, bot, "m3", "告警名称: 带宽告警")
    assert other is not None
    state = await db_session.get(
        BotAlertState, (source.id, "rule:网关异常"), populate_existing=True
    )
    assert state.repeats == 1
    # 上一轮还在跑：过了窗口也不开新一轮。
    past = datetime.now(UTC) - timedelta(hours=1)
    await db_session.execute(update(BotAlertState).values(triggered_at=past))
    await db_session.commit()
    assert await alert(db_session, bot, "m4", "告警名称: 网关异常") is None
    # 上一轮结束且过了窗口：开新一轮。
    await db_session.execute(update(Task).where(Task.id == first.id).values(status="succeeded"))
    await db_session.commit()
    assert await alert(db_session, bot, "m5", "告警名称: 网关异常") is not None


async def _event(session, bot, mid):
    return await session.scalar(
        select(InboundEvent).where(
            InboundEvent.bot_id == bot.id, InboundEvent.platform_msg_id == mid
        )
    )


async def test_speaker_is_service_user_and_rechecked(db_session):
    bot, service, source = await setup(db_session)
    await alert(db_session, bot, "m1", "告警名称: x")
    event = await _event(db_session, bot, "m1")
    speaker = await alert_sources.resolve_speaker(
        db_session, bot=bot, event=event, source_id=source.id
    )
    assert (
        speaker is not None and speaker.user_id == service.id and speaker.login_name == "svc-demo"
    )
    service.status = "disabled"
    await db_session.commit()
    assert (
        await alert_sources.resolve_speaker(db_session, bot=bot, event=event, source_id=source.id)
        is None
    )


async def test_speaker_rejects_other_bots_event(db_session):
    bot, _, source = await setup(db_session)
    await alert(db_session, bot, "m1", "x", union="on_other")
    event = await _event(db_session, bot, "m1")
    assert (
        await alert_sources.resolve_speaker(db_session, bot=bot, event=event, source_id=source.id)
        is None
    )


async def test_intake_cancels_when_service_user_disabled(db_engine, db_session):
    from coreman.runtime.worker.chat.handler import ChatTaskHandler

    bot, service, _ = await setup(db_session)
    task = await alert(db_session, bot, "m1", "告警名称: x")
    service.status = "disabled"
    await db_session.commit()
    await db_session.execute(update(Task).where(Task.id == task.id).values(status="running"))
    await db_session.commit()
    task = await db_session.get(Task, task.id, populate_existing=True)
    ctx = build_ctx(db_engine, task)
    await ChatTaskHandler().run(ctx)
    task = await db_session.get(Task, task.id, populate_existing=True)
    assert task.status == "cancelled" and task.error_code == "alert_source_unavailable"
    assert await db_session.get(Bot, bot.id) is not None


async def test_alert_turn_gets_no_write_credentials(db_engine, db_session):
    from coreman.core.cloud_proxy import policy
    from coreman.runtime.worker.chat.handler import ChatTaskHandler
    from tests.fakes.fake_relay import FakeRelay

    bot, _, _ = await setup(db_session, env={"SKILL_WRITE_TOKEN": "s3cret-value"})
    task = await alert(db_session, bot, "m1", "告警名称: x")
    await db_session.execute(update(Task).where(Task.id == task.id).values(status="running"))
    await db_session.commit()
    task = await db_session.get(Task, task.id, populate_existing=True)
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _r: fake.client())
    ctx.cloud_proxy_ready = True
    await ChatTaskHandler().run(ctx)
    env = fake.requests[0]["env_vars"]
    assert policy.TOKEN_ENV not in env and policy.URL_ENV not in env
    assert "SKILL_WRITE_TOKEN" not in env
    assert "s3cret-value" not in env.values()


def test_alert_key_priority():
    def build(text, content):
        return InboundMessage(
            platform="feishu",
            bot_id=uuid.uuid4(),
            kind="message",
            chat_type="group",
            chat_id=CHAT,
            sender={"platform_user_id": "", "sender_type": "bot"},
            message_id="m",
            parts=[{"type": "text", "text": text}],
            raw={"event": {"message": {"content": content}}},
            reply_context={},
        )

    assert (
        alert_sources.alert_key(build("**· 报警规则:** [Redis 带宽](http://x)", ""))
        == "rule:Redis 带宽"
    )
    assert alert_sources.alert_key(build("x", json.dumps({"title": "卡片"}))) == "title:卡片"
    assert alert_sources.alert_key(build("x", "")).startswith("hash:")
