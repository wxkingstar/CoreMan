"""网关的媒体与卡片纵切：引用透传入队、卡片事件建快车道任务、出站卡片与卡片更新。

复用 `test_gateway_wecom.py` 的夹具函数（`_bot`/`_start`/`_wait`），保证两边跑的是同一套
真连接、真租约的网关。
"""

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import outbox, streams
from coreman.core.bus.notify import notify
from coreman.core.db.models import BotLease, InboundEvent, OutboxItem, Task, TaskStream
from coreman.core.db.session import make_session_factory
from tests.fakes.fake_wecom_ws import FakeWeComWs
from tests.integration.test_gateway_wecom import _bot, _start, _wait

KEY = "k" * 32
CARD = {
    "card_type": "text_notice",
    "main_title": {"title": "t"},
    "card_action": {"type": 0},
    "task_id": "x@1",
}


async def _connected(fake: FakeWeComWs) -> None:
    await _wait(lambda: "bot1" in fake.connections)


async def test_media_message_is_enqueued_with_refs_only(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _connected(fake)
        await fake.send_media(
            "bot1", msgtype="image", url="https://media.test/1", aeskey=KEY, msgid="img-1"
        )
        await fake.send_media(
            "bot1",
            msgtype="mixed",
            items=[
                {"msgtype": "text", "text": {"content": "看图"}},
                {"msgtype": "image", "image": {"url": "https://media.test/2", "aeskey": KEY}},
            ],
            quote={"msgtype": "file", "file": {"url": "https://media.test/q", "aeskey": KEY}},
            msgid="mix-1",
        )

        async def two_tasks() -> bool:
            async with make_session_factory(db_engine)() as s:
                return len((await s.execute(select(Task))).scalars().all()) == 2

        await _wait(two_tasks)
        async with make_session_factory(db_engine)() as s:
            rows = (await s.execute(select(Task).order_by(Task.id))).scalars().all()
            assert [r.kind for r in rows] == ["chat", "chat"]
            img_parts = rows[0].payload["message"]["parts"]
            assert img_parts == [
                {"type": "image", "ref": {"url": "https://media.test/1", "aeskey": KEY}}
            ]
            mix_parts = rows[1].payload["message"]["parts"]
            assert (
                mix_parts[0] == {"type": "text", "text": "看图"} and mix_parts[1]["type"] == "image"
            )
            assert mix_parts[2]["type"] == "quote" and mix_parts[2]["kind"] == "file"
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_card_event_becomes_fast_task_and_dedupes(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _connected(fake)
        req = await fake.send_card_event(
            "bot1", task_id="choice@b@zs@1@0", selected={"choice_answer": ["opt_0"]}, msgid="evt-1"
        )
        await fake.send_card_event(
            "bot1", task_id="choice@b@zs@1@0", selected={"choice_answer": ["opt_0"]}, msgid="evt-1"
        )

        async def one_task() -> bool:
            async with make_session_factory(db_engine)() as s:
                return len((await s.execute(select(Task))).scalars().all()) >= 1

        await _wait(one_task)
        await asyncio.sleep(0.3)
        async with make_session_factory(db_engine)() as s:
            rows = (await s.execute(select(Task))).scalars().all()
            assert len(rows) == 1
            t = rows[0]
            assert t.kind == "card_action" and t.lane == "fast" and t.session_key == "zs"
            assert t.payload["card_action"]["task_id"] == "choice@b@zs@1@0"
            assert t.payload["card_action"]["selected"] == {"choice_answer": ["opt_0"]}
            ev = await s.get(InboundEvent, t.inbound_event_id)
            assert ev and ev.kind == "card_action" and ev.reply_context["req_id"] == req
            assert bot.id == t.bot_id
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_outbox_card_send_and_card_update(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _connected(fake)
        async with make_session_factory(db_engine)() as s:
            await outbox.add(
                s,
                bot_id=bot.id,
                platform="wecom",
                kind="send",
                dedupe_key="c1",
                target={"chat_id": "zs"},
                payload={"card": CARD},
            )
            await outbox.add(
                s,
                bot_id=bot.id,
                platform="wecom",
                kind="card_update",
                dedupe_key="u1",
                target={"req_id": "req-9", "task_id": "x@1"},
                payload={"card": CARD},
            )
            await outbox.add(
                s,
                bot_id=bot.id,
                platform="wecom",
                kind="card_update",
                dedupe_key="u2",
                target={"task_id": "x@1"},
                payload={"card": CARD},
            )
            await notify(s, "outbox_added", {"bot_id": str(bot.id)})
            await s.commit()
        await _wait(lambda: len(fake.sent_cards()) == 1 and len(fake.card_updates()) == 1)
        assert fake.sent_cards()[0] == {
            "chatid": "zs",
            "msgtype": "template_card",
            "template_card": CARD,
        }
        upd = fake.card_updates()[0]
        assert upd["headers"]["req_id"] == "req-9"
        assert upd["body"] == {"response_type": "update_template_card", "template_card": CARD}

        async def statuses() -> bool:
            async with make_session_factory(db_engine)() as s:
                rows = {
                    r.dedupe_key: r.status
                    for r in (await s.execute(select(OutboxItem))).scalars().all()
                }
                return rows == {"c1": "sent", "u1": "sent", "u2": "skipped"}

        await _wait(statuses)
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()


async def test_pending_card_is_sent_after_finish_frame(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    bot = await _bot(db_session)
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    service, runner = await _start(url)
    try:
        await _connected(fake)
        req = await fake.send_message("bot1", text="提问")

        async def task_ready() -> bool:
            async with make_session_factory(db_engine)() as s:
                return (await s.execute(select(Task))).scalars().first() is not None

        await _wait(task_ready)
        async with make_session_factory(db_engine)() as s:
            task = (await s.execute(select(Task))).scalars().one()
            ev = await s.get(InboundEvent, task.inbound_event_id)
            assert ev
            lease = await s.get(BotLease, bot.id)
            assert lease
            await streams.create(
                s,
                task_id=task.id,
                bot_id=bot.id,
                platform="wecom",
                stream_id="sid-1",
                reply_context=ev.reply_context,
                lease_generation=int(lease.generation),
                running_since=datetime.now(UTC),
            )
            await streams.complete(s, task.id, final_text="请选择", pending_card=CARD)
            await s.commit()
        await _wait(lambda: any(f for f in fake.stream_contents(req) if f[1]))
        await _wait(lambda: len(fake.sent_cards()) == 1)
        assert fake.sent_cards()[0]["template_card"] == CARD
        # 帧顺序：finish 先于卡片。
        cmds = [
            f["cmd"] for f in fake.frames if f.get("cmd") in ("aibot_respond_msg", "aibot_send_msg")
        ]
        assert cmds.index("aibot_send_msg") > max(
            i for i, c in enumerate(cmds) if c == "aibot_respond_msg"
        )

        async def card_confirmed() -> bool:
            async with make_session_factory(db_engine)() as s:
                item = (await s.execute(select(OutboxItem))).scalar_one()
                return item.status == "sent"

        await _wait(card_confirmed)
        async with make_session_factory(db_engine)() as s:
            rows = (await s.execute(select(OutboxItem))).scalars().all()
            assert [r.dedupe_key for r in rows] == [f"{task.id}:card:0"] and rows[
                0
            ].status == "sent"
            assert (await s.get(TaskStream, task.id)).finish_pushed_at is not None  # type: ignore[union-attr]
        assert uuid.UUID(str(bot.id))
    finally:
        service.request_stop("test")
        await asyncio.wait_for(runner, 15)
        await fake.stop()
