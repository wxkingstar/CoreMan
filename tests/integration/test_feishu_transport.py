import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from coreman.core.bus import instances, leases, outbox, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import (
    Bot,
    FeishuDelivery,
    FeishuSentMessage,
    OutboxItem,
    TaskStream,
    User,
)
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


async def test_long_stream_renews_streaming_instead_of_going_static(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        delivery.created_at = datetime.now(UTC) - timedelta(minutes=10)
        await streams.update(session, row.task_id, pending_text="after ten minutes")
        await session.commit()
    await transport.round()
    renewals = [
        json.loads(body["settings"])
        for method, path, body in api.calls
        if path == "/open-apis/cardkit/v1/cards/card1/settings"
    ]
    assert renewals and renewals[-1]["config"]["streaming_mode"] is True
    async with factory() as session:
        assert not (await session.get(FeishuDelivery, row.task_id)).is_static
    # 续开之后仍是逐字的流式文本更新，而不是整卡替换。
    assert any(
        method == "PUT"
        and path.endswith("/elements/answer/content")
        and body["content"] == "after ten minutes"
        for method, path, body in api.calls
    )
    assert not any(
        method == "PUT" and path.endswith("/cards/card1") for method, path, _ in api.calls
    )
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
        # 兜底入队的正文按 post 发，不再尝试卡片。
        assert final.payload == {
            "markdown": "visible",
            "_typing_task_id": row.task_id,
            "_plain": True,
        }
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


RICH_ANSWER = (
    "GMV 比上周 <font color='green'>↑12.6%</font>。\n\n"
    '```card:chart\n{"chart": "line", "title": "近 3 天", "x": ["a", "b", "c"], '
    '"series": [{"name": "GMV", "values": [1, 2, 3]}]}\n```'
)


def card_puts(api):
    return [
        json.loads(body["card"]["data"])
        for method, path, body in api.calls
        if method == "PUT" and path == "/open-apis/cardkit/v1/cards/card1"
    ]


def tags_of(node):
    if isinstance(node, dict):
        return ([node["tag"]] if "tag" in node else []) + [
            t for v in node.values() for t in tags_of(v)
        ]
    if isinstance(node, list):
        return [t for v in node for t in tags_of(v)]
    return []


async def finish(factory, row, answer):
    async with factory() as session:
        await streams.complete(session, row.task_id, final_text=answer)
        await session.commit()


async def test_final_answer_is_compiled_into_rich_card_with_summary(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    await finish(factory, row, RICH_ANSWER)
    await transport.round()
    [final] = card_puts(api)
    assert "chart" in tags_of(final)
    assert final["body"]["elements"][0]["tag"] == "collapsible_panel"
    assert final["config"]["summary"] == {"content": "GMV 比上周 ↑12.6%。"}
    settings = [
        json.loads(body["settings"])
        for method, path, body in api.calls
        if path == "/open-apis/cardkit/v1/cards/card1/settings"
    ]
    assert settings[-1]["config"] == {
        "streaming_mode": False,
        "summary": {"content": "GMV 比上周 ↑12.6%。"},
    }


async def test_rich_cards_switch_off_sends_plain_markdown_card(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    transport.rich_cards = False
    await transport.round()
    await finish(factory, row, RICH_ANSWER)
    await transport.round()
    [final] = card_puts(api)
    assert "chart" not in tags_of(final)
    body = json.dumps(final, ensure_ascii=False)
    assert "```card" not in body and "近 3 天" in body


async def test_rejected_rich_card_falls_back_to_plain_card_in_place(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class RejectRich(FakeAPI):
        async def call(self, method, path, **kwargs):
            body = kwargs.get("json") or {}
            if method == "PUT" and path.endswith("/cards/card1") and "chart" in json.dumps(body):
                self.calls.append((method, path, body))
                raise FeishuError(200220)
            return await super().call(method, path, **kwargs)

    api = RejectRich()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    await finish(factory, row, RICH_ANSWER)
    await transport.round()
    rich, plain = card_puts(api)
    assert "chart" in tags_of(rich) and "chart" not in tags_of(plain)
    async with factory() as session:
        assert (await session.get(TaskStream, row.task_id)).finish_pushed_at is not None
        assert not (await session.get(FeishuDelivery, row.task_id)).fallback
        assert await session.scalar(select(OutboxItem)) is None


async def test_long_answer_spills_into_continuation_cards(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    answer = "\n\n".join(f"## 第 {i} 节\n\n" + "很长的说明。" * 800 for i in range(12))
    await finish(factory, row, answer)
    await transport.round()
    async with factory() as session:
        more = list(await session.scalars(select(OutboxItem).order_by(OutboxItem.id)))
    assert more and all(item.payload["card"]["schema"] == "2.0" for item in more)
    assert [item.dedupe_key for item in more] == [
        f"{row.task_id}:more:{i}" for i in range(1, len(more) + 1)
    ]
    sent = [
        json.loads(body["content"])
        for _, path, body in api.calls
        if path == "/open-apis/im/v1/messages"
        and body.get("msg_type") == "interactive"
        and "schema" in json.loads(body["content"])
    ]
    assert len(sent) == len(more)
    assert "第 11 节" in json.dumps(sent[-1], ensure_ascii=False)


async def test_outbox_markdown_is_sent_as_card_and_falls_back_to_post(db_session, db_engine):
    bot, _row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    async with factory() as session:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key="cron:1",
            target={"chat_id": "oc1"},
            payload={"markdown": RICH_ANSWER},
        )
        await session.commit()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    sends = [b for _, p, b in api.calls if p == "/open-apis/im/v1/messages"]
    cards = [json.loads(b["content"]) for b in sends if b["msg_type"] == "interactive"]
    assert any("chart" in tags_of(c) for c in cards)

    class RejectCards(FakeAPI):
        async def call(self, method, path, **kwargs):
            body = kwargs.get("json") or {}
            if body.get("msg_type") == "interactive" and "schema" in body.get("content", ""):
                self.calls.append((method, path, body))
                raise FeishuError(200220)
            return await super().call(method, path, **kwargs)

    api = RejectCards()
    async with factory() as session:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key="cron:2",
            target={"chat_id": "oc1"},
            payload={"markdown": RICH_ANSWER},
        )
        await session.commit()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    posts = [
        b for _, p, b in api.calls if p == "/open-apis/im/v1/messages" and b["msg_type"] == "post"
    ]
    assert posts and "```card" not in posts[0]["content"]
    async with factory() as session:
        item = await session.scalar(select(OutboxItem).where(OutboxItem.dedupe_key == "cron:2"))
        assert item.status == "sent"


async def test_a_reply_that_crashes_rendering_does_not_block_other_replies(
    db_session, db_engine, monkeypatch
):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    async with factory() as session:
        task = await tasks.enqueue(session, NewTask(bot_id=bot.id, kind="chat", payload={}))
        other = await streams.create(
            session,
            task_id=task.id,
            bot_id=bot.id,
            platform="feishu",
            stream_id="s2",
            reply_context={"chat_id": "oc2", "message_id": "om_in2"},
            lease_generation=generation,
            running_since=datetime.now(UTC),
        )
        await streams.update(session, task.id, pending_text="second", thinking_md="t")
        await session.commit()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    real = transport.final_cards

    async def crash_first(thinking, answer, **kwargs):
        if answer == "poison":
            raise RuntimeError("renderer bug")
        return await real(thinking, answer, **kwargs)

    monkeypatch.setattr(transport, "final_cards", crash_first)
    await transport.round()
    await finish(factory, row, "poison")
    await finish(factory, other, "healthy answer")
    await transport.round()
    async with factory() as session:
        assert (await session.get(TaskStream, other.task_id)).finish_pushed_at is not None
        broken = await session.get(FeishuDelivery, row.task_id)
        assert broken.failures == 1 and broken.last_error == "RuntimeError"


async def test_rejected_continuation_card_is_resent_as_its_own_text_only(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class RejectContinuation(FakeAPI):
        async def call(self, method, path, **kwargs):
            body = kwargs.get("json") or {}
            content = body.get("content", "")
            if body.get("msg_type") == "interactive" and "第 11 节" in content:
                self.calls.append((method, path, body))
                raise FeishuError(230099)
            return await super().call(method, path, **kwargs)

    api = RejectContinuation()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    answer = "\n\n".join(f"## 第 {i} 节\n\n" + "很长的说明。" * 800 for i in range(12))
    await finish(factory, row, answer)
    await transport.round()
    posts = [
        b["content"]
        for _, p, b in api.calls
        if p == "/open-apis/im/v1/messages" and b.get("msg_type") == "post"
    ]
    assert posts and all("第 0 节" not in p for p in posts)
    assert any("第 11 节" in p for p in posts)
    async with factory() as session:
        assert {i.status for i in await session.scalars(select(OutboxItem))} == {"sent"}


async def test_outbox_rejection_mid_series_resends_only_that_card(db_session, db_engine):
    bot, _row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class RejectSecond(FakeAPI):
        async def call(self, method, path, **kwargs):
            body = kwargs.get("json") or {}
            if body.get("msg_type") == "interactive" and "第 6 节" in body.get("content", ""):
                self.calls.append((method, path, body))
                raise FeishuError(200860)
            return await super().call(method, path, **kwargs)

    api = RejectSecond()
    text = "\n\n".join(f"## 第 {i} 节\n\n" + "定时任务结果。" * 700 for i in range(10))
    async with factory() as session:
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key="cron:long",
            target={"chat_id": "oc1"},
            payload={"markdown": text},
        )
        await session.commit()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    sent = [b for _, p, b in api.calls if p == "/open-apis/im/v1/messages"]
    posts = [b["content"] for b in sent if b["msg_type"] == "post"]
    cards = [b["content"] for b in sent if b["msg_type"] == "interactive"]
    assert posts and all("第 0 节" not in p for p in posts)
    assert any("第 6 节" in p for p in posts)
    assert sum("第 0 节" in c for c in cards) == 1


def batch_actions(api):
    return [
        json.loads(body["actions"])
        for method, path, body in api.calls
        if path == "/open-apis/cardkit/v1/cards/card1/batch_update"
    ]


def content_puts(api, element_id=None):
    return [
        (path.split("/elements/")[1].split("/")[0], body["content"])
        for method, path, body in api.calls
        if method == "PUT"
        and path.endswith("/content")
        and (element_id is None or f"/elements/{element_id}/" in path)
    ]


async def test_finished_block_is_inserted_while_streaming(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    chart = '```card:chart\n{"chart": "bar", "x": ["a", "b"], "series": [{"values": [1, 2]}]}\n```'
    async with factory() as session:
        await streams.update(session, row.task_id, pending_text=f"结论先行。\n\n{chart}\n\n后文")
        await session.commit()
    await transport.round()
    [actions] = batch_actions(api)
    added = [e for a in actions if a["action"] == "add_elements" for e in a["params"]["elements"]]
    assert "chart" in tags_of(added)
    # 图表后面的正文是一个新的正文段，之后在它上面继续打字。
    text_ids = [e["element_id"] for e in added if e["tag"] == "markdown"]
    async with factory() as session:
        await streams.update(
            session, row.task_id, pending_text=f"结论先行。\n\n{chart}\n\n后文继续写"
        )
        await session.commit()
    await transport.round()
    assert content_puts(api, text_ids[-1])[-1][1] == "后文继续写"
    async with factory() as session:
        layout = (await session.get(FeishuDelivery, row.task_id)).layout
    assert [u["kind"] for u in layout["units"]] == ["text", "block", "text"]


async def test_unchanged_thinking_is_not_pushed_again(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    async with factory() as session:
        await streams.update(session, row.task_id, pending_text="hello world")
        await session.commit()
    await transport.round()
    assert len(content_puts(api, "thinking")) == 1


async def test_layout_drift_rebuilds_the_streaming_card(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class DriftOnce(FakeAPI):
        failed = False

        async def call(self, method, path, **kwargs):
            if path.endswith("/batch_update") and not self.failed:
                self.failed = True
                self.calls.append((method, path, kwargs.get("json") or {}))
                raise FeishuError(300314)
            return await super().call(method, path, **kwargs)

    api = DriftOnce()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    async with factory() as session:
        await streams.update(
            session,
            row.task_id,
            pending_text='前文\n\n```card:note\n{"text": "口径"}\n```\n\n后文',
        )
        await session.commit()
    await transport.round()
    [rebuilt] = card_puts(api)
    assert rebuilt["config"]["streaming_mode"] is True
    body = json.dumps(rebuilt, ensure_ascii=False)
    assert "口径" in body and "后文" in body
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        assert delivery.failures == 0 and len(delivery.layout["units"]) == 3


async def test_closed_stream_is_reopened_and_text_retried(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class ClosedOnce(FakeAPI):
        closed = True

        async def call(self, method, path, **kwargs):
            if path.endswith("/elements/answer/content") and self.closed:
                self.closed = False
                self.calls.append((method, path, kwargs.get("json") or {}))
                raise FeishuError(200850)
            return await super().call(method, path, **kwargs)

    api = ClosedOnce()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    settings = [
        json.loads(body["settings"]) for _, path, body in api.calls if path.endswith("/settings")
    ]
    assert settings and settings[-1]["config"]["streaming_mode"] is True
    assert content_puts(api, "answer")[-1][1] == "hello"
    sequences = [body["sequence"] for _, _, body in api.calls if "sequence" in body]
    assert sequences == sorted(set(sequences))


async def test_sequence_conflict_bumps_the_local_sequence(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory = make_session_factory(db_engine)

    class Behind(FakeAPI):
        async def call(self, method, path, **kwargs):
            if path.endswith("/content"):
                self.calls.append((method, path, kwargs.get("json") or {}))
                raise FeishuError(300317)
            return await super().call(method, path, **kwargs)

    transport = FeishuTransport(
        factory, Behind(), bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        assert delivery.sequence >= 1000 and delivery.failures == 1


class CardKit(FakeAPI):
    """记住卡片上根层级的组件：删不存在的、插到不存在的位置、写不存在的正文都按飞书报错。"""

    def __init__(self):
        super().__init__()
        self.roots: list[str] = []
        self.text: dict[str, str] = {}

    def _load(self, card):
        self.roots = [e["element_id"] for e in card["body"]["elements"]]
        self.text = {
            e["element_id"]: e["content"]
            for e in card["body"]["elements"]
            if e["tag"] == "markdown"
        }

    async def call(self, method, path, **kwargs):
        body = kwargs.get("json") or {}
        if path == "/open-apis/cardkit/v1/cards" and method == "POST":
            self._load(json.loads(body["data"]))
        elif method == "PUT" and path == "/open-apis/cardkit/v1/cards/card1":
            card = json.loads(body["card"]["data"])
            from coreman.core.feishu_cards.sanitize import validate_card

            if validate_card(card):
                self.calls.append((method, path, body))
                raise FeishuError(200220)
            self._load(card)
        elif path.endswith("/batch_update"):
            from coreman.core.feishu_cards.compile import card_shell
            from coreman.core.feishu_cards.sanitize import validate_card

            for action in json.loads(body["actions"]):
                params = action["params"]
                if action["action"] == "delete_elements":
                    if any(i not in self.roots for i in params["element_ids"]):
                        self.calls.append((method, path, body))
                        raise FeishuError(300314)
                    self.roots = [i for i in self.roots if i not in params["element_ids"]]
                else:
                    target = params["target_element_id"]
                    elements = params["elements"]
                    if target not in self.roots or validate_card(card_shell(elements)):
                        self.calls.append((method, path, body))
                        raise FeishuError(300315)
                    at = self.roots.index(target) + 1
                    self.roots[at:at] = [e["element_id"] for e in elements]
                    for e in elements:
                        if e["tag"] == "markdown":
                            self.text[e["element_id"]] = e["content"]
        elif method == "PUT" and path.endswith("/content"):
            element = path.split("/elements/")[1].split("/")[0]
            if element != "thinking" and element not in self.roots:
                self.calls.append((method, path, body))
                raise FeishuError(300314)
            self.text[element] = body["content"]
        return await super().call(method, path, **kwargs)


async def stream_steps(factory, transport, row, texts, thinking="think"):
    for pending in texts:
        async with factory() as session:
            await streams.update(session, row.task_id, pending_text=pending, thinking_md=thinking)
            await session.commit()
        await transport.round()
        async with factory() as session:
            delivery = await session.get(FeishuDelivery, row.task_id)
            assert delivery.failures == 0, delivery.last_error


async def test_thinking_only_first_push_keeps_the_answer_placeholder(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), CardKit()
    async with factory() as session:
        await streams.update(session, row.task_id, pending_text="")
        await session.commit()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    await stream_steps(factory, transport, row, ["", "第一句", "第一句，第二句"], thinking="step 2")
    assert "answer" in api.roots
    assert api.text["answer"] == "第一句，第二句"


async def test_reply_opening_with_a_header_streams_normally(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), CardKit()
    async with factory() as session:
        await streams.update(session, row.task_id, pending_text="")
        await session.commit()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    header = '```card:header\n{"title": "日报"}\n```\n\n'
    await stream_steps(
        factory, transport, row, [header[:20], header, header + "正文开始", header + "正文开始了"]
    )
    assert api.text["answer"] == "正文开始了"


async def test_samples_stream_without_layout_errors(db_session, db_engine):
    from coreman.core.feishu_cards.samples import SAMPLES

    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), CardKit()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    text = "".join(SAMPLES.values())
    await stream_steps(factory, transport, row, [text[:end] for end in range(40, len(text), 90)])
    assert not [
        c for c in api.calls if c[1] == "/open-apis/cardkit/v1/cards/card1" and c[0] == "PUT"
    ]


async def test_half_chart_over_quota_is_not_nested_in_a_row(db_session, db_engine):
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), CardKit()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    chart = (
        '```card:chart\n{"chart": "bar", "size": "half", "x": ["a", "b"], '
        '"series": [{"values": [1, 2]}]}\n```'
    )
    text = "\n\n".join([chart] * 8)
    await stream_steps(
        factory, transport, row, [text[:end] for end in range(60, len(text) + 60, 60)]
    )
    async with factory() as session:
        delivery = await session.get(FeishuDelivery, row.task_id)
        assert not delivery.layout.get("paused")


async def test_sent_messages_are_archived_for_later_quotes(db_session, db_engine):
    """流式回复收尾时存下终稿正文，出站消息发出时存下里面的文字；思考过程不进存档。"""
    bot, row, generation = await seed(db_session)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="old", generation=generation
    )
    await transport.round()
    async with factory() as session:
        # 流式卡片发出时只是卡片实体引用，还没有正文可存。
        assert await session.get(FeishuSentMessage, (bot.id, "om_1")) is None
        await streams.complete(session, row.task_id, final_text="最终答案")
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key="cron:1",
            target={"chat_id": "oc1"},
            payload={"markdown": "定时结果 42"},
        )
        await session.commit()
    await transport.round()
    async with factory() as session:
        rows = {r.message_id: r for r in (await session.scalars(select(FeishuSentMessage)))}
    assert rows.pop("om_1").text == "最终答案"
    assert [r.text for r in rows.values()] == ["定时结果 42"]
    assert all(r.chat_id == "oc1" for r in rows.values())
