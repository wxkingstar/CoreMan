"""助理应用的出站：一个进程按记录分发，租约围栏在应用上。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from coreman.core.assistants import keys, sealed
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.bus import app_leases, instances, outbox, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import (
    Bot,
    BotLease,
    FeishuDelivery,
    FeishuSentMessage,
    OutboxItem,
    PlatformApp,
    TaskStream,
    User,
)
from coreman.core.db.session import make_session_factory
from coreman.runtime.gateway_feishu.app_outbound import AppOutbound
from coreman.runtime.gateway_feishu.transport import FeishuTransport, LeaseLost
from tests.integration.assistant_helpers import raw
from tests.integration.test_feishu_transport import FakeAPI

pytestmark = pytest.mark.usefixtures("assistant_master")
NOTICE = "到点提醒"


@pytest.fixture(autouse=True)
def _no_call_spacing(monkeypatch):
    monkeypatch.setattr("coreman.runtime.gateway_feishu.transport.CALL_SPACING_SECONDS", 0)


async def _notice(session, bot, chat_id, key):
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=key,
        target={"chat_id": chat_id},
        payload={"text": NOTICE},
    )


async def _seed(session):
    app = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    session.add(app)
    await session.flush()
    await session.refresh(app)
    bots = []
    for login, name in (("zhangsan", "张三"), ("lisi", "李四"), ("wangwu", "王五")):
        owner = User(login_name=login, display_name=name)
        session.add(owner)
        await session.flush()
        # 开通时签发数据密钥，之后才有助理记录与它的内容行。
        await keys.ensure_key(session, sealed.master(), owner.id)
        bot = new_assistant_bot(
            owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc="x"
        )
        session.add(bot)
        bots.append(bot)
    zhang, li, wang = bots
    await session.flush()
    wang.enabled = False
    standard = Bot(
        bot_key="demo-bot",
        platform="feishu",
        name="demo-bot",
        created_by=zhang.owner_id,
        model="m",
        working_dir="/d",
        credentials_enc="unused",
    )
    session.add(standard)
    await instances.register(
        session, instance_id="gw-a", service="gateway-feishu", version="dev", capacity=None
    )
    await app_leases.ensure_rows(session)
    generation = await app_leases.acquire(session, app.id, "gw-a")
    await session.commit()
    return app, zhang, li, wang, standard, generation


async def _statuses(session):
    rows = await session.execute(select(OutboxItem.dedupe_key, OutboxItem.status))
    return dict(rows.all())


async def _stored(session, key):
    return str(await raw(session, "SELECT payload::text FROM outbox WHERE dedupe_key = :k", k=key))


async def test_one_round_delivers_for_every_enabled_assistant_of_the_app(db_session, db_engine):
    app, zhang, li, wang, standard, generation = await _seed(db_session)
    for bot, chat in ((zhang, "oc_zhang"), (li, "oc_li"), (wang, "oc_wang"), (standard, "oc_std")):
        await _notice(db_session, bot, chat, f"notice:{chat}")
    zhang.rich_cards = False
    await db_session.commit()
    api = FakeAPI()
    outbound = AppOutbound(
        make_session_factory(db_engine), api, app=app.id, instance_id="gw-a", generation=generation
    )
    assert {bot_id for bot_id, _ in await outbound.due()} == {zhang.id, li.id}
    assert await outbound.round() is False
    assert await _statuses(db_session) == {
        "notice:oc_zhang": "sent",
        "notice:oc_li": "sent",
        "notice:oc_wang": "pending",  # 停用的助理不发
        "notice:oc_std": "pending",  # 普通机器人的出站不归这个进程
    }
    sent_to = sorted(body["receive_id"] for _, path, body in api.calls if "receive_id" in body)
    assert sent_to == ["oc_li", "oc_zhang"]
    archived = (await db_session.scalars(select(FeishuSentMessage.bot_id))).all()
    assert sorted(archived, key=str) == sorted([zhang.id, li.id], key=str)
    assert outbound.transport(zhang.id).rich_cards is False
    assert outbound.transport(li.id).rich_cards is True
    # 没活了：不再碰任何一条记录。
    before = len(api.calls)
    assert await outbound.due() == []
    assert await outbound.round() is False and len(api.calls) == before
    # 助理记录不靠 bot 租约：全程没有造过一行。
    assert (await db_session.scalars(select(BotLease.bot_id))).all() == []


async def test_outbox_is_read_through_the_sealed_path(db_session, db_engine):
    app, zhang, _, _, standard, generation = await _seed(db_session)
    await _notice(db_session, zhang, "oc_zhang", "notice:zhang")
    await _notice(db_session, standard, "oc_std", "notice:std")
    await db_session.commit()
    # 库里：助理的载荷是所有者数据密钥的密文，普通机器人的仍是明文。
    # 载荷里的中文按 \u 转义与原样两种写法都查一遍。
    escaped = json.dumps(NOTICE)[1:-1]
    stored = await _stored(db_session, "notice:zhang")
    assert json.loads(stored)["_sealed"].startswith("enc:v2:d1:")
    assert NOTICE not in stored and escaped not in stored
    plain = await _stored(db_session, "notice:std")
    assert "_sealed" not in plain and (NOTICE in plain or escaped in plain)
    api = FakeAPI()
    outbound = AppOutbound(
        make_session_factory(db_engine), api, app=app.id, instance_id="gw-a", generation=generation
    )
    assert await outbound.round() is False
    # 发给平台的是明文；发完之后库里的载荷与正文存档仍是密文。
    sent = [body for _, _, body in api.calls if body.get("receive_id") == "oc_zhang"]
    assert len(sent) == 1 and NOTICE in sent[0]["content"]
    stored = await _stored(db_session, "notice:zhang")
    assert json.loads(stored)["_sealed"].startswith("enc:v2:d1:")
    assert NOTICE not in stored and escaped not in stored
    archived = str(
        await raw(db_session, "SELECT text FROM feishu_sent_messages WHERE bot_id = :b", b=zhang.id)
    )
    assert archived.startswith("enc:v2:d1:")


async def test_streams_are_pushed_per_assistant(db_session, db_engine):
    app, zhang, _, _, _, generation = await _seed(db_session)
    task = await tasks.enqueue(db_session, NewTask(bot_id=zhang.id, kind="chat", payload={}))
    await streams.create(
        db_session,
        task_id=task.id,
        bot_id=zhang.id,
        platform="feishu",
        stream_id="s",
        reply_context={"chat_id": "oc_zhang", "message_id": "om_in"},
        lease_generation=0,
        running_since=datetime.now(UTC),
    )
    await streams.update(db_session, task.id, pending_text="正在查", thinking_md="想一想")
    await db_session.commit()
    api = FakeAPI()
    outbound = AppOutbound(
        make_session_factory(db_engine), api, app=app.id, instance_id="gw-a", generation=generation
    )
    await outbound.round()
    assert any(path == "/open-apis/cardkit/v1/cards" for _, path, _ in api.calls)
    delivery = await db_session.get(FeishuDelivery, task.id)
    assert delivery is not None and delivery.message_id == "om_1"


async def test_one_assistant_failing_does_not_block_the_others(db_session, db_engine):
    app, zhang, li, _, _, generation = await _seed(db_session)
    await _notice(db_session, zhang, "oc_zhang", "notice:zhang")
    await _notice(db_session, li, "oc_li", "notice:li")
    await db_session.commit()
    outbound = AppOutbound(
        make_session_factory(db_engine),
        FakeAPI(),
        app=app.id,
        instance_id="gw-a",
        generation=generation,
    )

    async def boom():
        raise RuntimeError("synthetic")

    outbound.transport(li.id).round = boom
    assert await outbound.round() is False
    assert await _statuses(db_session) == {"notice:zhang": "sent", "notice:li": "pending"}


async def test_losing_the_app_lease_stops_every_delivery(db_session, db_engine):
    app, zhang, _, _, _, generation = await _seed(db_session)
    await _notice(db_session, zhang, "oc_zhang", "notice:zhang")
    await app_leases.release(db_session, app.id, "gw-a", generation)
    await db_session.commit()
    api = FakeAPI()
    outbound = AppOutbound(
        make_session_factory(db_engine), api, app=app.id, instance_id="gw-a", generation=generation
    )
    with pytest.raises(LeaseLost):
        await outbound.round()
    with pytest.raises(LeaseLost):
        await outbound.transport(zhang.id).round()
    assert api.calls == [] and await _statuses(db_session) == {"notice:zhang": "pending"}


async def _reply_in_progress(session, bot, chat_id):
    task = await tasks.enqueue(session, NewTask(bot_id=bot.id, kind="chat", payload={}))
    await streams.create(
        session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id=f"s-{chat_id}",
        reply_context={"chat_id": chat_id, "message_id": "om_in"},
        lease_generation=0,
        running_since=datetime.now(UTC),
    )
    await streams.update(session, task.id, bot_id=bot.id, pending_text="正在查")
    return task.id


async def test_unreadable_streams_are_closed_instead_of_staying_due(db_session, db_engine):
    app, zhang, _, _, standard, generation = await _seed(db_session)
    locked = await _reply_in_progress(db_session, zhang, "oc_zhang")
    plain = await _reply_in_progress(db_session, standard, "oc_std")
    await keys.destroy_key(db_session, zhang.owner_id)
    await db_session.commit()
    keys.forget()
    factory = make_session_factory(db_engine)
    api = FakeAPI()
    outbound = AppOutbound(factory, api, app=app.id, instance_id="gw-a", generation=generation)
    assert [bot_id for bot_id, _ in await outbound.due()] == [zhang.id]
    assert await outbound.round() is False
    # 数据密钥已销毁：这条回复永远读不出来，不推送，收尾之后不再算有活。
    assert api.calls == []
    assert await outbound.due() == []
    pushed = dict(
        (await db_session.execute(select(TaskStream.task_id, TaskStream.finish_pushed_at))).all()
    )
    assert pushed[locked] is not None and pushed[plain] is None

    async def no_fence():
        return None

    # 普通机器人读得出来的流照常推送，不会被当成读不出来的收尾。
    transport = FeishuTransport(
        factory, api, bot_id=standard.id, instance_id="gw-a", generation=1, lease_fence=no_fence
    )
    await transport.round()
    assert any(path == "/open-apis/cardkit/v1/cards" for _, path, _ in api.calls)
    still = await db_session.scalar(
        select(TaskStream.finish_pushed_at).where(TaskStream.task_id == plain)
    )
    assert still is None


_TRY_LOCK = text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))")


async def test_disabled_assistants_lose_their_transport_and_output_lock(db_session, db_engine):
    app, zhang, li, _, standard, generation = await _seed(db_session)
    await _notice(db_session, zhang, "oc_zhang", "notice:zhang")
    await _notice(db_session, li, "oc_li", "notice:li")
    await _notice(db_session, standard, "oc_std", "notice:std")
    await db_session.commit()
    factory = make_session_factory(db_engine)
    autocommit = db_engine.execution_options(isolation_level="AUTOCOMMIT")

    async def no_fence():
        return None

    async with autocommit.connect() as guard, autocommit.connect() as other:
        outbound = AppOutbound(
            factory, FakeAPI(), app=app.id, instance_id="gw-a", generation=generation, guard=guard
        )
        # 普通机器人的子进程同样把出站锁常驻在自己的守护连接上（这里借同一条连接）。
        ordinary = FeishuTransport(
            factory,
            FakeAPI(),
            bot_id=standard.id,
            instance_id="gw-a",
            generation=1,
            guard=guard,
            lease_fence=no_fence,
        )
        await outbound.round()
        await ordinary.round()
        assert set(outbound._transports) == {zhang.id, li.id}
        for bot in (zhang, li, standard):
            assert not await other.scalar(_TRY_LOCK, {"key": f"feishu-output:{bot.id}"})
        li.enabled = False
        await db_session.commit()
        # 每轮按名下启用的助理记录收拾一次：停用的那条 transport 移除，出站锁放掉。
        await outbound.retain({str(zhang.id)})
        assert set(outbound._transports) == {zhang.id}
        assert await other.scalar(_TRY_LOCK, {"key": f"feishu-output:{li.id}"})
        # 还在名下的记录、普通机器人的锁都不受影响。
        assert not await other.scalar(_TRY_LOCK, {"key": f"feishu-output:{zhang.id}"})
        assert not await other.scalar(_TRY_LOCK, {"key": f"feishu-output:{standard.id}"})
        # 重新启用后照常重建、重新拿锁（此刻锁在别的连接手里，这一轮先不发）。
        li.enabled = True
        await _notice(db_session, li, "oc_li", "notice:li-again")
        await db_session.commit()
        await outbound.round()
        assert (await _statuses(db_session))["notice:li-again"] == "pending"
        await other.execute(
            text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"),
            {"key": f"feishu-output:{li.id}"},
        )
        await outbound.round()
        assert (await _statuses(db_session))["notice:li-again"] == "sent"
