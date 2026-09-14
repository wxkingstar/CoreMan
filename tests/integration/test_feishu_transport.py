from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from coreman.core.db.models import Bot, FeishuDelivery, OutboxItem, TaskStream, User
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.feishu import FeishuError
from coreman.runtime.bus import instances, leases, outbox, streams, tasks
from coreman.runtime.bus.tasks import NewTask
from coreman.runtime.gateway_feishu.transport import FeishuTransport, LeaseLost


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.failure = None

    async def call(self, method, path, **kwargs):
        body = kwargs.get("json") or {}
        self.calls.append((method, path, body))
        if self.failure:
            raise FeishuError(self.failure)
        if path == "/open-apis/cardkit/v1/cards":
            return {"data": {"card_id": "card1"}}
        if path.endswith("/reply") or path == "/open-apis/im/v1/messages":
            mid = self.messages.setdefault(body["uuid"], f"om_{len(self.messages) + 1}")
            return {"data": {"message_id": mid}}
        return {"data": {}}


async def seed(session):
    user = User(display_name="test")
    session.add(user)
    await session.flush()
    bot = Bot(
        bot_key="feishu",
        platform="feishu",
        name="test",
        created_by=user.id,
        model="vllm/test",
        working_dir="/d",
        credentials_enc="unused",
    )
    session.add(bot)
    await session.flush()
    for name in ("old", "new"):
        await instances.register(
            session, instance_id=name, service="gateway-feishu", version="test", capacity=None
        )
    await leases.ensure_rows(session, "feishu")
    lease = await leases.acquire(session, bot_id=bot.id, platform="feishu", instance_id="old")
    task = await tasks.enqueue(session, NewTask(bot_id=bot.id, kind="chat", payload={}))
    row = await streams.create(
        session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s",
        reply_context={"chat_id": "oc1", "message_id": "om_in"},
        lease_generation=lease.generation,
        running_since=datetime.now(UTC),
    )
    await streams.update(session, task.id, pending_text="hello", thinking_md="think")
    await session.commit()
    await session.refresh(row)
    return bot, row, lease.generation


async def test_card_identity_and_sequence_survive_takeover(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    old = FeishuTransport(factory, api, bot_id=bot.id, instance_id="old", generation=generation)
    await old.round()
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        assert delivery.message_id == "om_1"
        first_sequence = delivery.sequence
        await leases.release(session, bot_id=bot.id, instance_id="old", generation=generation)
        lease = await leases.acquire(session, bot_id=bot.id, platform="feishu", instance_id="new")
        await streams.complete(session, row.task_id, final_text="completed")
        await session.commit()
        next_generation = lease.generation
    with pytest.raises(LeaseLost):
        await old.round()
    new = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="new", generation=next_generation
    )
    await new.round()
    assert len([c for c in api.calls if c[1] == "/open-apis/cardkit/v1/cards"]) == 1
    assert len(api.messages) == 1
    sequences = [body["sequence"] for _, _, body in api.calls if "sequence" in body]
    assert sequences == sorted(set(sequences)) and sequences[-1] > first_sequence
    async with factory() as session:
        assert (await session.get(TaskStream, row.task_id)).finish_pushed_at is not None
        assert (await session.get(FeishuDelivery, row.task_id)).is_static


async def test_nine_minute_stream_becomes_static_without_completion(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        delivery.created_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()
    await transport.round()
    async with factory() as session:
        assert (await session.get(FeishuDelivery, row.task_id)).is_static
        stream = await session.get(TaskStream, row.task_id)
        assert not stream.is_complete and stream.finish_pushed_at is None
    assert any(method == "PUT" and path.endswith("/cards/card1") for method, path, _ in api.calls)
    # 普通卡片在 10 分钟后继续接收增量与终稿，始终只发送一张卡片。
    async with factory() as session:
        await streams.update(session, row.task_id, pending_text="after ten minutes")
        await session.commit()
    await transport.round()
    assert any("after ten minutes" in str(body) for _, _, body in api.calls)
    async with factory() as session:
        await streams.complete(session, row.task_id, final_text="long task completed")
        await session.commit()
    await transport.round()
    assert any("long task completed" in str(body) for _, _, body in api.calls)
    assert len(api.messages) == 1
    async with factory() as session:
        assert (await session.get(TaskStream, row.task_id)).finish_pushed_at is not None
        assert not (await session.scalars(select(OutboxItem))).all()


async def test_outbox_permanent_platform_rejection_is_visible(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    async with factory() as session:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key="notify",
            target={"chat_id": "oc1"},
            payload={"markdown": "notice"},
        )
        await session.commit()
    api.failure = 230013
    await transport.consume_one()
    async with factory() as session:
        item = await session.scalar(select(OutboxItem))
        assert item.status == "failed" and "230013" in item.last_error


async def test_bad_card_does_not_block_notices_and_final_falls_back(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    original = api.call

    async def rejecting_card(method, path, **kwargs):
        if "/cardkit/" in path:
            raise FeishuError(230013)
        return await original(method, path, **kwargs)

    api.call = rejecting_card
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    async with factory() as session:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key="unrelated",
            target={"chat_id": "oc1"},
            payload={"text": "unrelated"},
        )
        await streams.complete(session, row.task_id, final_text="<think>internal</think>visible")
        await session.commit()
    await transport.round()
    async with factory() as session:
        assert (await session.scalar(select(OutboxItem))).status == "sent"
        assert (await session.get(FeishuDelivery, row.task_id)).fallback
    await transport.round()
    async with factory() as session:
        final = await session.scalar(select(OutboxItem).where(OutboxItem.dedupe_key != "unrelated"))
        assert final.status == "sent"
        assert final.payload == {"markdown": "visible"}
        assert (await session.get(TaskStream, row.task_id)).finish_pushed_at
