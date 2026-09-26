import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from coreman.core.bus import instances, leases, outbox, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, FeishuDelivery, OutboxItem, TaskStream, User
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.feishu import FeishuError
from coreman.runtime.gateway_feishu.transport import FeishuTransport, LeaseLost


@pytest.fixture(autouse=True)
def _no_call_spacing(monkeypatch):
    """频控间隔只为保护真实的飞书接口；FakeAPI 不限频，每次调用都等 0.3 秒只会拖慢用例。"""
    monkeypatch.setattr("coreman.runtime.gateway_feishu.transport.CALL_SPACING_SECONDS", 0)


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


async def test_round_checks_the_lease_fence_once(db_session, db_engine):
    """租约围栏每轮只在开头查一次：轮内每一次平台调用不再逐次回表。"""
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
            dedupe_key="notice",
            target={"chat_id": "oc1"},
            payload={"markdown": "notice"},
        )
        await session.commit()
    fences = 0
    real_fence = transport.fence

    async def counting_fence():
        nonlocal fences
        fences += 1
        await real_fence()

    transport.fence = counting_fence
    assert await transport.round() is False  # 出站发完了，不用接着来
    assert fences == 1 and len(api.calls) >= 4
    async with factory() as session:
        assert (await session.scalar(select(OutboxItem))).status == "sent"


async def test_output_lock_on_a_guard_connection_keeps_a_second_child_out(db_session, db_engine):
    """出站锁是常驻连接上的会话级锁：它不退出，同一 bot 的另一个 child 一张卡片都碰不了；
    连接本身不停在事务里，不会挡住迁移里的在线建索引。"""
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)
    first_api, second_api = FakeAPI(), FakeAPI()
    # 与子进程一致：守护连接来自 NullPool 的 AUTOCOMMIT 引擎，close 真正断开连接、释放会话级锁。
    guard_engine = create_async_engine(
        db_engine.url, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    async with guard_engine.connect() as first_guard, guard_engine.connect() as second_guard:
        first = FeishuTransport(
            factory,
            first_api,
            bot_id=bot.id,
            instance_id="old",
            generation=generation,
            guard=first_guard,
        )
        second = FeishuTransport(
            factory,
            second_api,
            bot_id=bot.id,
            instance_id="old",
            generation=generation,
            guard=second_guard,
        )
        await first.round()
        assert first_api.calls
        pid = await first_guard.scalar(text("SELECT pg_backend_pid()"))
        async with factory() as session:
            in_transaction = await session.scalar(
                text("SELECT xact_start IS NOT NULL FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
        assert in_transaction is False
        assert await second.round() is False and second_api.calls == []
        await first_guard.close()  # 第一个 child 退出：连接关闭，会话级锁随之释放
        async with factory() as session:
            await streams.complete(session, row.task_id, final_text="done")
            await session.commit()
        await second.round()
        assert second_api.calls
    await guard_engine.dispose()


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
        assert final.payload == {"markdown": "visible", "_typing_task_id": row.task_id}
        assert (await session.get(TaskStream, row.task_id)).finish_pushed_at


async def test_typing_survives_thinking_and_failed_answer_then_cleans_after_delivery(
    db_session, db_engine
):
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class ReactionAPI(FakeAPI):
        reject_answer = False

        async def call(self, method, path, **kwargs):
            if path.endswith("/reactions"):
                self.calls.append((method, path, kwargs.get("json")))
                return {"data": {"reaction_id": "reaction1"}}
            if self.reject_answer and path.endswith("/elements/answer/content"):
                raise FeishuError(999)
            return await super().call(method, path, **kwargs)

    api = ReactionAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    async with factory() as session:
        await streams.update(session, row.task_id, pending_text="", thinking_md="working")
        await session.commit()
    await transport.round()
    assert any(
        path.endswith("/reactions") and body["reaction_type"]["emoji_type"] == "Typing"
        for _, path, body in api.calls
    )
    assert not any(method == "DELETE" for method, _, _ in api.calls)
    async with factory() as session:
        await streams.update(session, row.task_id, pending_text="actual answer")
        await session.commit()
    api.reject_answer = True
    await transport.round()
    assert not any(method == "DELETE" for method, _, _ in api.calls)
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        delivery.retry_at = None
        await session.commit()
    api.reject_answer = False
    # Restart must reuse the saved reaction and clean it only after the answer is delivered.
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    assert sum(path.endswith("/reactions") for _, path, _ in api.calls) == 1
    answer_index = max(
        i for i, (_, path, _) in enumerate(api.calls) if path.endswith("/elements/answer/content")
    )
    delete_index = next(i for i, (method, _, _) in enumerate(api.calls) if method == "DELETE")
    assert answer_index < delete_index
    await transport.round()
    assert sum(path.endswith("/reactions") for _, path, _ in api.calls) == 1


async def test_typing_cleanup_retries_after_stream_is_finished(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class CleanupAPI(FakeAPI):
        reject_delete = True

        async def call(self, method, path, **kwargs):
            if path.endswith("/reactions"):
                return {"data": {"reaction_id": "reaction1"}}
            if method == "DELETE" and self.reject_delete:
                raise FeishuError(999)
            return await super().call(method, path, **kwargs)

    api = CleanupAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    async with factory() as session:
        await streams.complete(session, row.task_id, final_text="done")
        await session.commit()
    await transport.round()
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        assert delivery.reaction_id == "reaction1" and delivery.reaction_done
        assert (await session.get(TaskStream, row.task_id)).finish_pushed_at
        delivery.reaction_retry_at = None
        await session.commit()
    api.reject_delete = False
    await transport.round()
    async with factory() as session:
        assert (await session.get(FeishuDelivery, row.task_id)).reaction_id is None


async def test_missing_reaction_permission_does_not_block_answer(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class NoPermissionAPI(FakeAPI):
        async def call(self, method, path, **kwargs):
            if path.endswith("/reactions"):
                raise FeishuError(99991672)
            return await super().call(method, path, **kwargs)

    api = NoPermissionAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        assert delivery.message_id and not delivery.fallback and delivery.failures == 0
        assert delivery.reaction_done
    assert any(body.get("content") == "hello" for _, _, body in api.calls)


async def test_card_only_answer_waits_for_outbox_delivery(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class CardAPI(FakeAPI):
        fail_card = True

        async def call(self, method, path, **kwargs):
            if path.endswith("/reactions"):
                return {"data": {"reaction_id": "reaction1"}}
            if path == "/open-apis/im/v1/messages" and self.fail_card:
                raise FeishuError(999)
            return await super().call(method, path, **kwargs)

    api = CardAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    async with factory() as session:
        await streams.update(session, row.task_id, pending_text="")
        await streams.complete(
            session,
            row.task_id,
            final_text="",
            pending_card={
                "schema": "2.0",
                "body": {"elements": [{"tag": "markdown", "content": "请选择"}]},
            },
        )
        await session.commit()
    await transport.round()
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        assert delivery.reaction_id == "reaction1" and not delivery.reaction_done
        item = await session.scalar(select(OutboxItem))
        item.not_before = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    api.fail_card = False
    await transport.round()
    async with factory() as session:
        assert (await session.get(FeishuDelivery, row.task_id)).reaction_done


async def test_fallback_thinking_only_completion_cleans_typing(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    async with factory() as session:
        session.add(FeishuDelivery(task_id=row.task_id, fallback=True, reaction_id="reaction1"))
        await streams.complete(session, row.task_id, final_text="<think>only process</think>")
        await session.commit()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        assert delivery.reaction_done and delivery.reaction_id is None
        assert not list(await session.scalars(select(OutboxItem)))


async def test_permanent_outbox_failure_ends_typing(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class PermanentAPI(FakeAPI):
        async def call(self, method, path, **kwargs):
            if path == "/open-apis/im/v1/messages":
                raise FeishuError(230013)
            return await super().call(method, path, **kwargs)

    async with factory() as session:
        session.add(FeishuDelivery(task_id=row.task_id, fallback=True, reaction_id="reaction1"))
        await streams.complete(session, row.task_id, final_text="answer")
        await session.commit()
    transport = FeishuTransport(
        factory, PermanentAPI(), bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    async with factory() as session:
        assert (await session.scalar(select(OutboxItem))).status == "failed"
        delivery = await session.get(FeishuDelivery, row.task_id)
        assert delivery.reaction_done and delivery.reaction_id is None


async def test_thinking_heading_animates_without_new_content(db_session, db_engine, monkeypatch):
    bot, row, generation = await seed(db_session)
    await streams.update(db_session, row.task_id, pending_text="", thinking_md="waiting")
    await db_session.commit()
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    clock = [100.0]
    monkeypatch.setattr(
        "coreman.runtime.gateway_feishu.transport.heading_clock", lambda: clock[0], raising=False
    )
    await transport.round()
    api.calls.clear()
    clock[0] += 3
    await transport.round()
    patches = [
        body for method, path, body in api.calls if path.endswith("/elements/thinking_panel")
    ]
    import json

    assert json.loads(patches[-1]["partial_element"])["header"]["title"]["content"] == "🤔 思考中.."
    assert not any(path.endswith("/content") for _, path, _ in api.calls)
    api.calls.clear()
    await transport.round()
    assert not any(path.endswith("/elements/thinking_panel") for _, path, _ in api.calls)
    await streams.update(db_session, row.task_id, pending_text="answer")
    await db_session.commit()
    await transport.round()
    patches = [body for _, path, body in api.calls if path.endswith("/elements/thinking_panel")]
    assert json.loads(patches[-1]["partial_element"])["header"]["title"]["content"] == "🤔 思考过程"


class ImageAPI(FakeAPI):
    async def call(self, method, path, **kwargs):
        if path == "/open-apis/im/v1/images":
            self.calls.append((method, path, {"files": kwargs.get("files"), **kwargs["data"]}))
            return {"data": {"image_key": "img_v3_up"}}
        return await super().call(method, path, **kwargs)


def image_host():
    import httpx

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    return httpx.MockTransport(lambda request: httpx.Response(200, content=png))


async def test_final_answer_embeds_uploaded_image_and_keeps_download_link(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), ImageAPI()
    transport = FeishuTransport(
        factory,
        api,
        bot_id=bot.id,
        instance_id="old",
        generation=generation,
        image_transport=image_host(),
    )
    url = "https://oss.example/avatar.png?Signature=s"
    async with factory() as session:
        await streams.update(session, row.task_id, pending_text=f"头像：![头像]({url})")
        await session.commit()
    await transport.round()
    # 流式期间不下载，远程图片先显示成链接，不是裂图。
    assert not [c for c in api.calls if c[1] == "/open-apis/im/v1/images"]
    streamed = [c[2]["content"] for c in api.calls if c[1].endswith("/elements/answer/content")]
    assert streamed[-1] == f"头像：[🖼️ 头像]({url})"
    async with factory() as session:
        await streams.complete(session, row.task_id, final_text=f"头像：![头像]({url})")
        await session.commit()
    await transport.round()
    uploads = [c for c in api.calls if c[1] == "/open-apis/im/v1/images"]
    assert len(uploads) == 1 and uploads[0][2]["image_type"] == "message"
    final = json.loads(
        next(c for c in reversed(api.calls) if c[0] == "PUT" and c[1].endswith("/cards/card1"))[2][
            "card"
        ]["data"]
    )
    assert final["body"]["elements"][1]["content"] == (
        f"头像：![头像](img_v3_up)\n[查看原图]({url})"
    )


async def test_fallback_post_sends_image_as_native_node(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), ImageAPI()
    transport = FeishuTransport(
        factory,
        api,
        bot_id=bot.id,
        instance_id="old",
        generation=generation,
        image_transport=image_host(),
    )
    url = "https://oss.example/chart.png"
    async with factory() as session:
        session.add(FeishuDelivery(task_id=row.task_id, fallback=True))
        await streams.complete(session, row.task_id, final_text=f"图表如下\n![图表]({url})")
        await session.commit()
    await transport.round()
    posts = [
        json.loads(c[2]["content"])
        for c in api.calls
        if c[1] == "/open-apis/im/v1/messages" and c[2].get("msg_type") == "post"
    ]
    assert posts[-1]["zh_cn"]["content"] == [
        [{"tag": "md", "text": "图表如下"}],
        [{"tag": "img", "image_key": "img_v3_up"}],
        [{"tag": "md", "text": f"[查看原图]({url})"}],
    ]
