"""飞书群话题：话题各有各的会话，第一次在话题里找机器人时问要不要继承群主会话。"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import instances, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import topics
from coreman.core.db.models import (
    Bot,
    ChatSession,
    InboundEvent,
    OutboxItem,
    RuntimeNode,
    Task,
)
from coreman.core.i18n.messages import msg
from coreman.core.wecom.messages import InboundMessage
from coreman.runtime.gateway_common.inbound import enqueue_inbound
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from tests.fakes.fake_relay import FakeRelay
from tests.integration.worker_helpers import build_ctx, seed_bot, turn_block

CHAT = "oc_group"
THREAD = "omt_topic"
TOPIC = topics.session_key(CHAT, THREAD)


async def _feishu_bot(session: AsyncSession) -> Bot:
    bot, _r, _c = await seed_bot(session)
    bot.platform = "feishu"
    await session.commit()
    return bot


async def _say(
    session: AsyncSession,
    bot: Bot,
    text: str,
    *,
    user: str = "zs",
    thread: str | None = THREAD,
) -> Task | None:
    mid = f"om-{uuid.uuid4()}"
    message = InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="message",
        chat_type="group",
        chat_id=CHAT,
        sender={"platform_user_id": user, "sender_type": "user"},
        message_id=mid,
        mentions_bot=True,
        parts=[{"type": "text", "text": text}],
        reply_context={
            "chat_id": CHAT,
            "chat_type": "group",
            "message_id": mid,
            "thread_id": thread,
        },
    )
    snapshot = SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None)
    task = await enqueue_inbound(session, snapshot, message, lease_generation=1)
    await session.commit()
    return task


async def _main_session(session: AsyncSession, bot: Bot) -> ChatSession:
    row = ChatSession(
        bot_id=bot.id,
        session_key=CHAT,
        relay_session_id=uuid.uuid4(),
        backend="claude",
        last_active_at=datetime.now(UTC),
    )
    session.add(row)
    await session.commit()
    return row


async def _card(session: AsyncSession) -> OutboxItem:
    card = await session.scalar(
        select(OutboxItem)
        .where(OutboxItem.kind == "send", OutboxItem.dedupe_key.like("%:topic"))
        .order_by(OutboxItem.id.desc())
    )
    assert card is not None
    return card


async def _click(
    engine: AsyncEngine, session: AsyncSession, bot: Bot, target: Task, key: str, *, user="zs"
) -> OutboxItem | None:
    # 飞书只认已经发出去的卡：模拟网关发送成功，回填平台消息 id。
    offer = await _card(session)
    offer.status = "sent"
    offer.payload = {**offer.payload, "_feishu_message_id": "om-card"}
    await session.commit()
    message = InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="card_action",
        chat_type="group",
        chat_id=CHAT,
        sender={"platform_user_id": user},
        message_id=f"action:{uuid.uuid4()}",
        card_action={
            "task_id": topics.card_task_id(target.id),
            "card_type": "form",
            "event_key": key,
        },
        reply_context={"chat_id": CHAT, "message_id": "om-card"},
    )
    snapshot = SimpleNamespace(id=bot.id, bot_key=bot.bot_key, welcome_message=None)
    task = await enqueue_inbound(session, snapshot, message, lease_generation=1)
    await session.commit()
    assert task is not None
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    claimed = await tasks.claim(session, lane="fast", instance_id="worker-test")
    await session.commit()
    assert claimed and claimed.id == task.id
    await CardActionHandler().run(build_ctx(engine, claimed))
    return await session.scalar(
        select(OutboxItem).where(OutboxItem.kind == "card_update").order_by(OutboxItem.id.desc())
    )


async def _refresh(session: AsyncSession, task: Task) -> Task:
    row = await session.get(Task, task.id, populate_existing=True)
    assert row is not None
    return row


async def _topic_session(session: AsyncSession, bot: Bot) -> ChatSession | None:
    return await session.get(ChatSession, (bot.id, TOPIC), populate_existing=True)


def test_session_keys() -> None:
    assert topics.key_for("feishu", "group", CHAT, {"thread_id": THREAD}) == TOPIC
    assert topics.key_for("feishu", "group", CHAT, {"thread_id": None}) == CHAT
    assert topics.key_for("feishu", "single", "ou_x", {"thread_id": THREAD}) == "ou_x"
    assert topics.key_for("wecom", "group", CHAT, {"thread_id": THREAD}) == CHAT
    assert topics.chat_of(TOPIC) == CHAT and topics.chat_of(CHAT) == CHAT
    assert topics.is_topic(TOPIC) and not topics.is_topic(CHAT)


async def test_main_chat_message_keeps_the_group_session(db_session: AsyncSession) -> None:
    bot = await _feishu_bot(db_session)
    await _main_session(db_session, bot)
    task = await _say(db_session, bot, "看看昨天的数据", thread=None)
    assert task is not None and task.session_key == CHAT
    assert not task.payload.get("topic_hold")


async def test_topic_without_a_group_session_starts_fresh_without_asking(
    db_session: AsyncSession,
) -> None:
    bot = await _feishu_bot(db_session)
    task = await _say(db_session, bot, "话题里的问题")
    assert task is not None and task.session_key == TOPIC
    assert not task.payload.get("topic_hold")
    assert await db_session.scalar(select(OutboxItem)) is None


async def test_first_topic_message_is_held_and_asks(db_session: AsyncSession) -> None:
    bot = await _feishu_bot(db_session)
    await _main_session(db_session, bot)
    task = await _say(db_session, bot, "话题里的问题")
    assert task is not None and task.session_key == TOPIC
    assert task.payload["topic_hold"] is True
    assert task.run_after > datetime.now(UTC)
    card = await _card(db_session)
    assert card.payload["_reply"] is True
    assert card.target == {"chat_id": CHAT, "message_id": task.payload["message"]["message_id"]}
    assert card.payload["card"] == topics.offer_card(task.id)
    buttons = [e for e in card.payload["card"]["body"]["elements"] if e["tag"] == "button"]
    assert [b["text"]["content"] for b in buttons] == [
        msg("topic_opt_new"),
        msg("topic_opt_fork"),
    ]
    # 挂起期间不会被认领。
    await instances.register(
        db_session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    assert await tasks.claim(db_session, lane="normal", instance_id="worker-test") is None


async def test_fork_choice_records_the_group_session(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _feishu_bot(db_session)
    main = await _main_session(db_session, bot)
    task = await _say(db_session, bot, "话题里的问题")
    assert task is not None
    reply = await _click(db_engine, db_session, bot, task, topics.FORK)
    row = await _topic_session(db_session, bot)
    assert row is not None and row.forked_from == main.relay_session_id
    assert row.relay_session_id != main.relay_session_id
    task = await _refresh(db_session, task)
    assert "topic_hold" not in task.payload and task.run_after <= datetime.now(UTC)
    assert reply is not None
    assert reply.payload["card"]["header"]["title"]["content"] == msg("topic_fork_title")


async def test_new_choice_starts_an_independent_session(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _feishu_bot(db_session)
    await _main_session(db_session, bot)
    task = await _say(db_session, bot, "话题里的问题")
    assert task is not None
    reply = await _click(db_engine, db_session, bot, task, topics.NEW)
    row = await _topic_session(db_session, bot)
    assert row is not None and row.forked_from is None
    assert "topic_hold" not in (await _refresh(db_session, task)).payload
    assert reply is not None
    assert reply.payload["card"]["header"]["title"]["content"] == msg("topic_new_title")
    # 选过就不再问。
    again = await _say(db_session, bot, "接着问")
    assert again is not None and not again.payload.get("topic_hold")


async def test_only_the_sender_can_choose(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot = await _feishu_bot(db_session)
    await _main_session(db_session, bot)
    task = await _say(db_session, bot, "话题里的问题")
    assert task is not None
    await _click(db_engine, db_session, bot, task, topics.FORK, user="ls")
    assert await _topic_session(db_session, bot) is None
    assert (await _refresh(db_session, task)).payload["topic_hold"] is True


async def test_another_message_counts_as_independent(db_session: AsyncSession) -> None:
    bot = await _feishu_bot(db_session)
    await _main_session(db_session, bot)
    first = await _say(db_session, bot, "话题里的问题")
    assert first is not None
    second = await _say(db_session, bot, "补充一句")
    assert second is not None and second.session_key == TOPIC
    assert second.payload["serialize_session"] is True
    assert not second.payload.get("topic_hold") and not second.payload.get("busy_offer")
    first = await _refresh(db_session, first)
    assert "topic_hold" not in first.payload and first.run_after <= datetime.now(UTC)
    row = await _topic_session(db_session, bot)
    assert row is not None and row.forked_from is None
    # 只发过一张话题卡，没有「上一条还在处理」卡。
    sends = (await db_session.scalars(select(OutboxItem).where(OutboxItem.kind == "send"))).all()
    assert len(sends) == 1


async def test_a_late_click_says_it_has_started(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _feishu_bot(db_session)
    await _main_session(db_session, bot)
    first = await _say(db_session, bot, "话题里的问题")
    assert first is not None
    await _say(db_session, bot, "补充一句")
    reply = await _click(db_engine, db_session, bot, first, topics.FORK)
    assert reply is not None
    assert reply.payload["card"]["header"]["title"]["content"] == msg("topic_late_title")
    row = await _topic_session(db_session, bot)
    assert row is not None and row.forked_from is None


async def test_topic_commands_apply_to_the_topic(db_session: AsyncSession) -> None:
    bot = await _feishu_bot(db_session)
    await _main_session(db_session, bot)
    task = await _say(db_session, bot, "/new")
    assert task is not None and task.kind == "command" and task.session_key == TOPIC
    assert await db_session.scalar(select(OutboxItem)) is None


# ---- 对话回合 -------------------------------------------------------------


async def _chat_task(session: AsyncSession, bot: Bot, text: str, key: str, **payload) -> Task:  # type: ignore[no-untyped-def]
    mid = f"m-{uuid.uuid4()}"
    m = {
        "platform": bot.platform,
        "kind": "message",
        "chat_type": "group",
        "chat_id": CHAT,
        "sender": {"platform_user_id": "zs", "open_id": None},
        "message_id": mid,
        "mentions_bot": True,
        "parts": [{"type": "text", "text": text}],
        "card_action": None,
        "raw": {},
    }
    ev = InboundEvent(
        bot_id=bot.id,
        platform=bot.platform,
        platform_msg_id=mid,
        kind="message",
        chat_type="group",
        chat_id=CHAT,
        sender_platform_user_id="zs",
        payload=m,
        reply_context={"gateway_instance": "gw", "req_id": f"r-{mid}"},
    )
    session.add(ev)
    await session.flush()
    t = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="chat",
            payload={"message": m, "bot_key": bot.bot_key, "platform_user_id": "zs", **payload},
            session_key=key,
            inbound_event_id=ev.id,
        ),
    )
    await session.commit()
    assert t
    await instances.register(
        session, instance_id="worker-test", service="worker", version="dev", capacity=8
    )
    claimed = await tasks.claim(session, lane="normal", instance_id="worker-test")
    await session.commit()
    assert claimed and claimed.id == t.id
    return claimed


async def _run(engine: AsyncEngine, task: Task, relay: FakeRelay) -> None:
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await ChatTaskHandler().run(ctx)
    await ctx.chat_logs.drain(5)


async def _group_bot(session: AsyncSession, *, fork: bool) -> Bot:
    # 对话回合与平台无关：用企微群消息走通，话题键与分叉记录照样生效。
    bot, relay, _c = await seed_bot(session)
    if fork:
        node = await session.get(RuntimeNode, relay.runtime_node_id)
        assert node is not None
        node.capabilities = {
            **node.capabilities,
            "claude": {**node.capabilities["claude"], topics.RUNTIME_CAPABILITY: True},
        }
        await session.commit()
    return bot


async def test_forked_topic_asks_the_runtime_to_fork_once(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _group_bot(db_session, fork=True)
    fake = FakeRelay("normal")
    await _run(db_engine, await _chat_task(db_session, bot, "群里的第一问", CHAT), fake)
    main_sid = fake.requests[0]["session_id"]
    assert await topics.choose(db_session, bot.id, TOPIC, fork=True)
    await db_session.commit()

    await _run(db_engine, await _chat_task(db_session, bot, "话题里的问题", TOPIC), fake)
    body = fake.requests[1]
    assert body["session_id"] != main_sid
    assert body["fork_session_id"] == main_sid
    # 群主会话在这台节点上找不到时驱动才用：不进本轮块。
    assert "群里的第一问" in body["resume_fallback"]
    assert "群里的第一问" not in turn_block(body)
    row = await db_session.get(ChatSession, (bot.id, TOPIC), populate_existing=True)
    assert row is not None and row.forked_from is None

    await _run(db_engine, await _chat_task(db_session, bot, "话题里接着问", TOPIC), fake)
    follow = fake.requests[2]
    assert follow["session_id"] == body["session_id"]
    assert "fork_session_id" not in follow and "resume_fallback" not in follow
    # 群主会话不受影响。
    main = await db_session.get(ChatSession, (bot.id, CHAT), populate_existing=True)
    assert main is not None and str(main.relay_session_id) == main_sid


async def test_old_runtime_gets_the_group_conversation_as_text(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _group_bot(db_session, fork=False)
    fake = FakeRelay("normal")
    await _run(db_engine, await _chat_task(db_session, bot, "群里的第一问", CHAT), fake)
    assert await topics.choose(db_session, bot.id, TOPIC, fork=True)
    await db_session.commit()
    await _run(db_engine, await _chat_task(db_session, bot, "话题里的问题", TOPIC), fake)
    body = fake.requests[1]
    assert "fork_session_id" not in body
    assert "群里的第一问" in turn_block(body)
    assert "从群里的主会话分出来" in turn_block(body)


async def test_failed_first_turn_forks_again(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _group_bot(db_session, fork=True)
    fake = FakeRelay("normal")
    await _run(db_engine, await _chat_task(db_session, bot, "群里的第一问", CHAT), fake)
    main_sid = fake.requests[0]["session_id"]
    assert await topics.choose(db_session, bot.id, TOPIC, fork=True)
    await db_session.commit()
    fake.scenario = "relay_error"
    await _run(db_engine, await _chat_task(db_session, bot, "话题里的问题", TOPIC), fake)
    fake.scenario = "normal"
    await _run(db_engine, await _chat_task(db_session, bot, "再问一次", TOPIC), fake)
    assert fake.requests[2]["fork_session_id"] == main_sid
    assert fake.requests[2]["session_id"] == fake.requests[1]["session_id"]


async def test_expired_hold_runs_as_independent(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _group_bot(db_session, fork=True)
    fake = FakeRelay("normal")
    await _run(db_engine, await _chat_task(db_session, bot, "群里的第一问", CHAT), fake)
    task = await _chat_task(db_session, bot, "话题里的问题", TOPIC, topic_hold=True)
    await _run(db_engine, task, fake)
    body = fake.requests[1]
    assert "fork_session_id" not in body
    assert body["session_id"] != fake.requests[0]["session_id"]
    row = await db_session.get(ChatSession, (bot.id, TOPIC), populate_existing=True)
    assert row is not None and row.forked_from is None
    assert "topic_hold" not in (await _refresh(db_session, task)).payload
