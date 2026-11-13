"""bus 层之外直接读写内容的几处：巡检补流与收尸、合并排队消息、引用回复取原文、凭证副本擦除。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import sealed
from coreman.core.bus import outbox, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, InboundEvent
from coreman.core.db.session import make_session_factory
from coreman.core.i18n.messages import msg
from coreman.runtime.scheduler.reaper import reap_lost_tasks, recover_terminal_streams
from coreman.runtime.worker.chat import feishu_quote, queued_batch
from coreman.runtime.worker.credential_cards import wipe
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
MESSAGE = {"message_type": "text", "content": json.dumps({"text": SECRET}, ensure_ascii=False)}


async def _event(
    session: AsyncSession, bot: Bot, payload: dict[str, Any], msg_id: str = "om_1"
) -> int:
    stored = await sealed.seal_values(session, InboundEvent, bot.id, {"payload": payload})
    event_id = await session.scalar(
        insert(InboundEvent)
        .values(
            bot_id=bot.id,
            platform="feishu",
            platform_msg_id=msg_id,
            kind="message",
            chat_type="single",
            chat_id="oc_1",
            payload=stored["payload"],
            reply_context={},
        )
        .returning(InboundEvent.id)
    )
    return int(event_id)


async def _streaming(session: AsyncSession, bot: Bot, payload: dict[str, Any]) -> int:
    task = await tasks.enqueue(
        session, NewTask(bot_id=bot.id, kind="chat", payload=payload, session_key="oc_1")
    )
    assert task is not None
    await streams.create(
        session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s1",
        reply_context={"chat_id": "oc_1"},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    await streams.update(session, task.id, bot_id=bot.id, pending_text=f"半截：{SECRET}")
    return task.id


async def test_recover_terminal_streams_carries_the_partial_text(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _streaming(db_session, bot, {})
    await tasks.finish(db_session, task_id, status="succeeded")
    await db_session.execute(
        text("UPDATE tasks SET finished_at = now() - interval '5 minutes' WHERE id = :id"),
        {"id": task_id},
    )
    await db_session.commit()
    # 另开会话：巡检拿到的是库里读出、尚未还原的行。
    async with make_session_factory(db_engine)() as scheduler:
        assert await recover_terminal_streams(scheduler, datetime.now(UTC)) == 1
        await scheduler.commit()
    stored = await raw(
        db_session, "SELECT final_text FROM task_streams WHERE task_id = :id", id=task_id
    )
    assert stored.startswith("enc:v2:d1:")
    async with make_session_factory(db_engine)() as other:
        row = await streams.get(other, task_id)
        assert row is not None and row.is_complete and row.final_text == f"半截：{SECRET}"


async def test_reaping_a_lost_task_reads_the_payload_and_the_partial_text(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id = await _streaming(
        db_session, bot, {"message": {"chat_id": "oc_9", "chat_type": "single", "text": SECRET}}
    )
    await db_session.execute(
        text(
            "UPDATE tasks SET status = 'running', claimed_at = now() - interval '5 minutes',"
            " heartbeat_at = now() - interval '5 minutes' WHERE id = :id"
        ),
        {"id": task_id},
    )
    await db_session.commit()
    factory = make_session_factory(db_engine)
    async with factory() as scheduler:
        assert await reap_lost_tasks(scheduler, datetime.now(UTC), chat_logs_factory=factory) == 1
        await scheduler.commit()
    async with factory() as other:
        row = await streams.get(other, task_id)
        assert row is not None and row.is_complete
        assert row.final_text == f"半截：{SECRET}\n\n" + msg("worker_lost")
    # 对话记录里的会话取自任务载荷里的消息：读到的是明文才取得到。
    assert (
        await raw(db_session, "SELECT chat_id FROM chat_logs WHERE task_id = :id", id=task_id)
        == "oc_9"
    )
    dumped = str(
        await raw(
            db_session,
            "SELECT concat_ws('|', (SELECT final_text FROM task_streams WHERE task_id = :id),"
            " (SELECT string_agg(payload::text, '|') FROM outbox),"
            " (SELECT string_agg(concat_ws('|', message_content, error_message), '|')"
            "  FROM chat_logs))",
            id=task_id,
        )
    )
    assert SECRET not in dumped


@pytest.mark.parametrize("seed", ["assistant", "standard"])
async def test_queued_messages_are_merged_from_readable_payloads(
    db_engine: AsyncEngine, db_session: AsyncSession, seed: str
) -> None:
    bot = (
        (await seed_assistant(db_session))[0]
        if seed == "assistant"
        else await seed_standard(db_session)
    )
    ids: list[int] = []
    for index, words in enumerate(["第一条", f"第二条 {SECRET}"]):
        event_id = await _event(db_session, bot, {}, msg_id=f"om_{index}")
        task = await tasks.enqueue(
            db_session,
            NewTask(
                bot_id=bot.id,
                kind="chat",
                session_key="oc_1",
                inbound_event_id=event_id,
                payload={
                    "platform_user_id": "ou_1",
                    "message": {"parts": [{"type": "text", "text": words}]},
                },
            ),
        )
        assert task is not None
        ids.append(task.id)
    await db_session.commit()
    async with make_session_factory(db_engine)() as worker:
        head = await tasks.get(worker, ids[0])
        intake = SimpleNamespace(
            session_key="oc_1",
            bot=bot,
            text="第一条",
            inbound=SimpleNamespace(received_at=datetime.now(UTC) - timedelta(minutes=1)),
        )
        merged_into, entries = await queued_batch.gather(
            worker,
            SimpleNamespace(task=head),  # type: ignore[arg-type]
            intake,  # type: ignore[arg-type]
        )
        await worker.commit()
    assert merged_into.id == ids[0]
    assert [entry.text for entry in entries] == ["第一条", f"第二条 {SECRET}"]
    stored = str(await raw(db_session, "SELECT string_agg(payload::text, '|') FROM tasks"))
    assert (SECRET in stored) is (seed == "standard")
    async with make_session_factory(db_engine)() as other:
        second = await tasks.get(other, ids[1])
        assert second is not None and second.status == "cancelled"
        assert second.result == {"merged_into": ids[0]}


@pytest.mark.parametrize("seed", ["assistant", "standard"])
async def test_quote_reads_the_original_from_inbound_and_outbox(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, seed: str
) -> None:
    bot = (
        (await seed_assistant(db_session))[0]
        if seed == "assistant"
        else await seed_standard(db_session)
    )
    await _event(db_session, bot, {"raw": {"event": {"message": MESSAGE}}})
    item = await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key="9:send:final",
        target={"chat_id": "oc_1"},
        payload={"markdown": f"答：{SECRET}"},
    )
    assert item is not None
    item.payload = {**item.payload, "_feishu_message_id": "om_2"}
    await outbox.mark_sent(db_session, item.id)
    await db_session.commit()
    monkeypatch.setattr(feishu_quote, "render", lambda kind, content, names=None: (kind, content))
    assert await feishu_quote._from_inbound(
        db_session, bot=bot, chat_id="oc_1", parent_id="om_1"
    ) == ("text", MESSAGE["content"])
    assert (
        await feishu_quote.persisted_text(db_session, bot=bot, chat_id="oc_1", parent_id="om_2")
        == f"答：{SECRET}"
    )


@pytest.mark.parametrize("seed", ["assistant", "standard"])
async def test_wipe_removes_the_sealed_copy(
    db_engine: AsyncEngine, db_session: AsyncSession, seed: str
) -> None:
    bot = (
        (await seed_assistant(db_session))[0]
        if seed == "assistant"
        else await seed_standard(db_session)
    )
    action = {"task_id": "cred:1", "sealed": "copy-of-the-submission"}
    event_id = await _event(db_session, bot, {"card_action": action})
    task = await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind="card_action", payload={"card_action": action}),
    )
    await db_session.commit()
    assert task is not None
    factory = make_session_factory(db_engine)
    async with factory() as session:
        await wipe(session, task.id, event_id)
        await session.commit()
    async with factory() as session:
        got = await tasks.get(session, task.id)
        event = await session.get(InboundEvent, event_id)
        assert got is not None and got.payload["card_action"] == {"task_id": "cred:1"}
        assert event is not None and event.payload["card_action"] == {"task_id": "cred:1"}
