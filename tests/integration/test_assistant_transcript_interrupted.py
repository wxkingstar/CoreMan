"""被打断的一轮：节点在后台上传转录，下一轮开轮前等它一小会儿，等到了就续上原会话。"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import platform_turns, transcripts
from coreman.core.assistants.kinds import HANDOVER_KIND
from coreman.core.db.models import Bot, RelayServer, Task
from coreman.core.db.session import make_session_factory
from tests.fakes.fake_relay import FakeRelay
from tests.integration.job_helpers import take
from tests.integration.test_assistant_platform_turn_handler import Handover, assistant
from tests.integration.worker_helpers import build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master")
BLOB = b"\x1f\x8b-transcript"


async def turn(
    engine: AsyncEngine, session: AsyncSession, bot: Bot, index: int
) -> tuple[Task, dict[str, object]]:
    """跑一轮平台来源的任务，返回结束后的任务与发给节点的请求体。"""
    queued = await platform_turns.enqueue(
        session, bot, HANDOVER_KIND, {"note": f"第 {index} 件"}, dedupe_key=f"handover:{index}"
    )
    await session.commit()
    assert queued is not None
    task = await take(session, queued.id)
    relay = FakeRelay("normal")
    ctx = build_ctx(engine, task, relay_client_factory=lambda _r: relay.client())
    await Handover().run(ctx)
    await ctx.chat_logs.drain(5)
    done = await session.get(Task, task.id, populate_existing=True)
    assert done is not None and done.status == "succeeded"
    (request,) = relay.requests
    return done, request


async def node_of(session: AsyncSession, bot: Bot) -> uuid.UUID:
    relay = await session.get(RelayServer, bot.relay_server_id)
    assert relay is not None and relay.runtime_node_id is not None
    return relay.runtime_node_id


async def returned(engine: AsyncEngine, bot: Bot, request: dict[str, object]) -> str:
    """节点把这一轮的转录交回来（用它自己的连接，与开轮的那一边并行）。"""
    body = request["transcript"]
    assert isinstance(body, dict)
    async with make_session_factory(engine)() as other:
        result = await transcripts.upload(
            other,
            node_id=await node_of(other, bot),
            transfer=uuid.UUID(str(body["transfer"])),
            status="ok",
            data=BLOB,
        )
        await other.commit()
    return result


async def interrupted(
    engine: AsyncEngine, session: AsyncSession, bot: Bot, *, ago: float = 0.0
) -> tuple[dict[str, object], dict[str, object]]:
    """一轮正常交接，再一轮到过节点后被取消（转录还没传回来）；返回两轮的请求体。"""
    _, first = await turn(engine, session, bot, 1)
    assert await returned(engine, bot, first) == "stored"
    task, second = await turn(engine, session, bot, 2)
    assert second["session_id"] == first["session_id"]
    assert second["transcript"]["restore"] is True  # type: ignore[index]
    await session.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(status="cancelled", finished_at=datetime.now(UTC) - timedelta(seconds=ago))
    )
    await session.commit()
    return first, second


async def test_a_late_upload_from_a_cancelled_turn_is_waited_for(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    first, second = await interrupted(db_engine, db_session, bot)

    async def late() -> None:
        await asyncio.sleep(0.6)
        assert await returned(db_engine, bot, second) == "stored"

    uploading = asyncio.create_task(late())
    started = time.monotonic()
    _, third = await turn(db_engine, db_session, bot, 3)
    await uploading
    # 等到了：还是原来的会话，带着上一轮刚传回来的转录续。
    assert time.monotonic() - started >= 0.5
    assert third["session_id"] == first["session_id"]
    assert third["transcript"]["restore"] is True  # type: ignore[index]


async def test_an_upload_that_never_comes_starts_a_new_session_after_the_wait(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert transcripts.UPLOAD_WAIT == timedelta(seconds=20)
    monkeypatch.setattr(transcripts, "UPLOAD_WAIT", timedelta(seconds=1))
    bot, _ = await assistant(db_session)
    first, second = await interrupted(db_engine, db_session, bot)
    started = time.monotonic()
    _, third = await turn(db_engine, db_session, bot, 3)
    waited = time.monotonic() - started
    assert 0.7 <= waited < 10
    # 与原来一样：换新会话，不还原；迟到的上传对不上号。
    assert third["session_id"] != first["session_id"]
    assert third["transcript"]["restore"] is False  # type: ignore[index]
    assert await returned(db_engine, bot, second) == "stale"


async def test_a_turn_cancelled_long_ago_is_not_waited_for(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    first, _ = await interrupted(db_engine, db_session, bot, ago=21)
    started = time.monotonic()
    _, third = await turn(db_engine, db_session, bot, 3)
    assert time.monotonic() - started < 5
    assert third["session_id"] != first["session_id"]


async def test_a_released_turn_is_not_waited_for(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """没到过节点的轮次（排队时就撤销了）：交接编号已作废，不等，上一份照旧可续。"""
    bot, _ = await assistant(db_session)
    first, second = await interrupted(db_engine, db_session, bot)
    released = await transcripts.release(
        db_session,
        bot_id=bot.id,
        session_id=uuid.UUID(str(first["session_id"])),
        transfer=uuid.UUID(str(second["transcript"]["transfer"])),  # type: ignore[index]
    )
    await db_session.commit()
    assert released
    assert (
        await transcripts.awaiting_upload(
            db_session, bot_id=bot.id, session_key="oc_private", task_id=0
        )
        is None
    )
    started = time.monotonic()
    _, third = await turn(db_engine, db_session, bot, 3)
    assert time.monotonic() - started < 5
    assert third["session_id"] == first["session_id"]
    assert third["transcript"]["restore"] is True  # type: ignore[index]


async def test_only_a_cancelled_turn_is_waited_for(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await assistant(db_session)
    _, first = await turn(db_engine, db_session, bot, 1)
    assert await returned(db_engine, bot, first) == "stored"
    # 正常结束的一轮在收尾之前就该传完：编号还在就是没传上来，不等。
    done, second = await turn(db_engine, db_session, bot, 2)
    key = {"bot_id": bot.id, "session_key": "oc_private"}
    assert await transcripts.awaiting_upload(db_session, task_id=0, **key) is None
    # 被取消的才等，从它结束的时刻起算；问的是它自己时不算。
    finished = datetime.now(UTC)
    await db_session.execute(
        update(Task).where(Task.id == done.id).values(status="cancelled", finished_at=finished)
    )
    await db_session.commit()
    until = await transcripts.awaiting_upload(db_session, task_id=0, **key)
    assert until == finished + transcripts.UPLOAD_WAIT
    assert await transcripts.awaiting_upload(db_session, task_id=done.id, **key) is None
    late = finished + timedelta(seconds=20)
    assert await transcripts.awaiting_upload(db_session, task_id=0, now=late, **key) is None
    # 转录到了就不用等了。
    assert await returned(db_engine, bot, second) == "stored"
    assert await transcripts.awaiting_upload(db_session, task_id=0, **key) is None
