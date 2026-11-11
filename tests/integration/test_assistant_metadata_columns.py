"""正文之外会带出内容的三列：流的最近动作、出站的报错说明、任务的取消原因。

最近动作的说明文字封存，时间与状态留明文；后两列运维接口与控制逻辑要直接读，不能封存，
助理的行在写入时只留开头的代码。普通机器人的行原样不变。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import outbox, streams, tasks
from coreman.core.bus.tasks import Heartbeat, NewTask
from coreman.core.db.models import Bot, OutboxItem, Task
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
ACTIVITY = {
    "at": "2026-11-11T02:00:00+00:00",
    "label": f"执行命令：grep {SECRET}",
    "until": "2026-11-11T02:05:00+00:00",
    "expect": "等待 5 分钟",
    "stuck": True,
}


async def _bot(session: AsyncSession, seed: str) -> Bot:
    if seed == "assistant":
        return (await seed_assistant(session))[0]
    return await seed_standard(session)


async def _task(session: AsyncSession, bot: Bot) -> int:
    task = await tasks.enqueue(
        session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="oc_1")
    )
    assert task is not None
    return task.id


async def _stream(session: AsyncSession, bot: Bot) -> int:
    task_id = await _task(session, bot)
    await streams.create(
        session,
        task_id=task_id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s1",
        reply_context={"chat_id": "oc_1"},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    return task_id


async def _item(session: AsyncSession, bot: Bot, key: str) -> OutboxItem:
    item = await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=key,
        target={"chat_id": "oc_1"},
        payload={"markdown": "你好"},
    )
    assert item is not None
    return item


def test_only_the_leading_code_survives() -> None:
    assert sealed.code_only(f"Feishu 230001: 内容「{SECRET}」不合法") == "Feishu 230001"
    assert sealed.code_only(f"not_sent: {SECRET}") == "not_sent"
    assert sealed.code_only("content_locked") == "content_locked"
    assert sealed.code_only("TimeoutError") == "TimeoutError"
    assert sealed.code_only("recipient binding changed") == "recipient"
    assert sealed.code_only(f"{SECRET}") == "redacted"
    assert sealed.code_only(f"对方说：{SECRET}", fallback="cancelled") == "cancelled"
    assert sealed.code_only("x" * 200) == "redacted"


async def test_stream_activity_keeps_times_plain_and_seals_the_label(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id = await _stream(db_session, bot)
    await streams.update(db_session, task_id, bot_id=bot.id, activity=ACTIVITY)
    await db_session.commit()
    stored = await raw(
        db_session, "SELECT activity FROM task_streams WHERE task_id = :id", id=task_id
    )
    assert SECRET not in str(stored) and "label" not in stored
    assert stored["_sealed"].startswith("enc:v2:d1:")
    assert {k: v for k, v in stored.items() if k != "_sealed"} == {
        k: v for k, v in ACTIVITY.items() if k != "label"
    }
    factory = make_session_factory(db_engine)
    async with factory() as other:
        assert (await streams.pending_for_bot(other, bot.id))[0].activity == ACTIVITY
    # 收尾时整份替换（带中断原因）同样封存。
    await streams.complete(
        db_session,
        task_id,
        final_text="",
        activity={**ACTIVITY, "interrupted": "处理进程失联"},
        bot_id=bot.id,
    )
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    stored = await raw(
        db_session, "SELECT activity FROM task_streams WHERE task_id = :id", id=task_id
    )
    assert SECRET not in str(stored) and stored["interrupted"] == "处理进程失联"
    async with factory() as other:
        row = await streams.get(other, task_id)
        # 密钥销毁之后：说明文字读不出来，时间与中断原因还在。
        assert row is not None and "label" not in row.activity
        assert row.activity["at"] == ACTIVITY["at"]
        assert row.activity["interrupted"] == "处理进程失联"


async def test_standard_bot_activity_is_unchanged(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    task_id = await _stream(db_session, bot)
    await streams.update(db_session, task_id, bot_id=bot.id, activity=ACTIVITY)
    await db_session.commit()
    assert (
        await raw(db_session, "SELECT activity FROM task_streams WHERE task_id = :id", id=task_id)
        == ACTIVITY
    )


@pytest.mark.parametrize("seed", ["assistant", "standard"])
async def test_outbox_errors_keep_only_the_code_for_assistants(
    db_session: AsyncSession, seed: str
) -> None:
    bot = await _bot(db_session, seed)
    detail = f"Feishu 230001: 内容「{SECRET}」不合法"
    retried = await _item(db_session, bot, "1:a")
    assert await outbox.mark_failed(db_session, retried.id, detail) == "pending"
    failed = await _item(db_session, bot, "2:a")
    await outbox.fail(db_session, failed.id, detail)
    skipped = await _item(db_session, bot, "3:a")
    await outbox.mark_skipped(db_session, skipped.id, f"协作已结束：{SECRET}")
    locked = await _item(db_session, bot, "4:a")
    await outbox.mark_skipped(db_session, locked.id, sealed.LOCKED_CODE)
    bounced = await _item(db_session, bot, "5:a")
    await outbox.mark_sent(db_session, bounced.id)
    assert await outbox.rejected(db_session, bounced.id, error=detail, retry_after=None)
    unknown = await _item(db_session, bot, "6:a")
    await db_session.execute(
        update(OutboxItem).where(OutboxItem.id == unknown.id).values(status="sending")
    )
    assert await outbox.unknown_attempt(db_session, unknown.id, f"delivery_unknown: {SECRET}")
    await db_session.commit()
    errors = await raw(db_session, "SELECT array_agg(last_error ORDER BY id) FROM outbox")
    if seed == "standard":
        assert errors == [
            detail,
            detail,
            f"协作已结束：{SECRET}",
            sealed.LOCKED_CODE,
            detail,
            f"delivery_unknown: {SECRET}",
        ]
    else:
        assert errors == [
            "Feishu 230001",
            "Feishu 230001",
            "redacted",
            sealed.LOCKED_CODE,
            "Feishu 230001",
            "delivery_unknown",
        ]
    statuses = await raw(db_session, "SELECT array_agg(status ORDER BY id) FROM outbox")
    assert statuses == ["pending", "failed", "skipped", "skipped", "failed", "pending"]


@pytest.mark.parametrize("seed", ["assistant", "standard"])
async def test_cancel_reason_keeps_only_a_code_for_assistants(
    db_session: AsyncSession, seed: str
) -> None:
    bot = await _bot(db_session, seed)
    free_text = f"对方答复：{SECRET}"
    first = await _task(db_session, bot)
    second = await _task(db_session, bot)
    assert await tasks.request_cancel(db_session, first, free_text)
    assert await tasks.request_cancel(db_session, second, "user_stop")
    await db_session.execute(update(Task).values(status="running"))
    await db_session.commit()
    reasons = await raw(db_session, "SELECT array_agg(cancel_reason ORDER BY id) FROM tasks")
    assert reasons == [free_text if seed == "standard" else "cancelled", "user_stop"]
    # worker 从心跳拿到的就是库里这份。
    assert await tasks.heartbeat(db_session, first) == (Heartbeat.CANCELLED, reasons[0])
