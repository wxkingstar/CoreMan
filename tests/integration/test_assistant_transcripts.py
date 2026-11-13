"""assistant_transcripts：库里是密文；交接编号决定续不续；对不上号、读不出来都从新会话开始。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed, transcripts
from coreman.core.chat import sessions
from coreman.core.db.models import AssistantTranscript, ChatSession
from coreman.core.db.session import make_session_factory
from tests.fakes.runtime_node import add_node
from tests.integration.assistant_helpers import SECRET, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
BLOB = b"\x1f\x8b-transcript-" + SECRET.encode()


async def start(session: AsyncSession, bot, node_id, *, key: str = "oc_owner"):
    info = await sessions.get_or_create(
        session,
        bot_id=bot.id,
        session_key=key,
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    out = await transcripts.prepare(
        session,
        bot=bot,
        node_id=node_id,
        info=info,
        session_key=key,
        backend="claude",
        speaker_user_id=None,
    )
    await session.commit()
    return out


async def test_turns_hand_over_and_resume(db_session: AsyncSession):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node = await add_node(db_session)
    info, plan = await start(db_session, bot, node.id)
    assert info.is_new and plan.restore is False
    assert plan.body() == {"transfer": plan.transfer.hex, "restore": False}
    assert await transcripts.download(db_session, node_id=node.id, transfer=plan.transfer) is None
    result = await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    assert result == "stored"
    # 交接办完：编号作废，同一编号再传是 stale。
    again = await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    assert again == "stale"
    second, plan2 = await start(db_session, bot, node.id)
    assert second.relay_session_id == info.relay_session_id and plan2.restore is True
    got = await transcripts.download(db_session, node_id=node.id, transfer=plan2.transfer)
    assert got == BLOB
    # 节点没跑这一轮（拒绝了）：上一份照旧可用。
    kept = await transcripts.upload(
        db_session, node_id=node.id, transfer=plan2.transfer, status="unchanged", data=b""
    )
    await db_session.commit()
    assert kept == "kept"
    third, plan3 = await start(db_session, bot, node.id)
    assert third.relay_session_id == info.relay_session_id and plan3.restore is True


@pytest.mark.parametrize("status", ["empty", "too_large"])
async def test_no_usable_transcript_means_a_new_session(db_session: AsyncSession, status):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node = await add_node(db_session)
    info, plan = await start(db_session, bot, node.id)
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    _, plan2 = await start(db_session, bot, node.id)
    assert (
        await transcripts.upload(
            db_session, node_id=node.id, transfer=plan2.transfer, status=status, data=b""
        )
        == "cleared"
    )
    await db_session.commit()
    renewed, plan3 = await start(db_session, bot, node.id)
    assert renewed.is_new and renewed.relay_session_id != info.relay_session_id
    assert plan3.restore is False
    old = await db_session.get(AssistantTranscript, (bot.id, info.relay_session_id))
    assert old is None


async def test_a_turn_whose_transcript_never_came_back_is_not_resumed(db_session: AsyncSession):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node = await add_node(db_session)
    info, plan = await start(db_session, bot, node.id)
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    _, lost = await start(db_session, bot, node.id)  # 这一轮的转录没有传回来
    assert lost.restore is True
    after, plan3 = await start(db_session, bot, node.id)
    assert after.is_new and after.relay_session_id != info.relay_session_id
    assert plan3.restore is False
    # 迟到的上传对不上号，不会改动任何东西。
    late = await transcripts.upload(
        db_session, node_id=node.id, transfer=lost.transfer, status="ok", data=BLOB
    )
    assert late == "stale"
    row = await db_session.get(ChatSession, (bot.id, "oc_owner"), populate_existing=True)
    assert row is not None and row.relay_session_id == after.relay_session_id


async def test_another_node_cannot_take_or_replace_it(db_session: AsyncSession):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node, stranger = await add_node(db_session), await add_node(db_session, name="other")
    _, plan = await start(db_session, bot, node.id)
    assert (
        await transcripts.upload(
            db_session, node_id=stranger.id, transfer=plan.transfer, status="ok", data=BLOB
        )
        == "stale"
    )
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    _, plan2 = await start(db_session, bot, node.id)
    assert (
        await transcripts.download(db_session, node_id=stranger.id, transfer=plan2.transfer) is None
    )


async def test_destroyed_key_or_tampered_blob_starts_fresh(
    db_engine: AsyncEngine, db_session: AsyncSession
):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node = await add_node(db_session)
    info, plan = await start(db_session, bot, node.id)
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    _, plan2 = await start(db_session, bot, node.id)
    await db_session.execute(
        text(
            "UPDATE assistant_transcripts SET blob_enc = overlay(blob_enc placing '\\x00'::bytea"
            " from length(blob_enc)) WHERE bot_id = :b"
        ),
        {"b": bot.id},
    )
    await db_session.commit()
    async with make_session_factory(db_engine)() as request_session:
        sealed.tolerate_undecryptable(request_session)
        got = await transcripts.download(request_session, node_id=node.id, transfer=plan2.transfer)
        assert got is None
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan2.transfer, status="unchanged", data=b""
    )
    await db_session.commit()
    renewed, plan3 = await start(db_session, bot, node.id)
    assert renewed.relay_session_id != info.relay_session_id and plan3.restore is False


async def test_purge_drops_only_old_rows(db_session: AsyncSession):
    bot = await seed_standard(db_session)
    old, fresh = uuid.uuid4(), uuid.uuid4()
    db_session.add_all(
        [
            AssistantTranscript(
                bot_id=bot.id, session_id=old, updated_at=datetime.now(UTC) - timedelta(days=9)
            ),
            AssistantTranscript(bot_id=bot.id, session_id=fresh),
        ]
    )
    await db_session.commit()
    assert await transcripts.purge(db_session, datetime.now(UTC) - timedelta(days=4)) == 1
    await db_session.commit()
    assert await db_session.get(AssistantTranscript, (bot.id, fresh)) is not None


async def test_late_or_repeated_uploads_never_change_what_is_stored(db_session: AsyncSession):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node = await add_node(db_session)
    info, plan = await start(db_session, bot, node.id)
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.commit()
    _, plan2 = await start(db_session, bot, node.id)
    assert (
        await transcripts.upload(
            db_session, node_id=node.id, transfer=plan2.transfer, status="unchanged", data=b""
        )
        == "kept"
    )
    await db_session.commit()
    # 已回报 unchanged 之后迟到的上传（被拒绝那一轮事后才清扫出来的）：不能覆盖、也不能清空。
    for status, data in (("ok", b"late-copy"), ("empty", b""), ("ok", b"late-copy")):
        late = await transcripts.upload(
            db_session, node_id=node.id, transfer=plan2.transfer, status=status, data=data
        )
        assert late == "stale"
    # 没发过的编号同样对不上号。
    unknown = await transcripts.upload(
        db_session, node_id=node.id, transfer=uuid.uuid4(), status="ok", data=b"x"
    )
    assert unknown == "stale"
    await db_session.commit()
    _, plan3 = await start(db_session, bot, node.id)
    assert plan3.restore is True
    assert await transcripts.download(db_session, node_id=node.id, transfer=plan3.transfer) == BLOB
    assert await transcripts.download(db_session, node_id=node.id, transfer=plan2.transfer) is None


async def test_purge_works_in_bounded_batches(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(transcripts, "PURGE_BATCH", 2)
    bot = await seed_standard(db_session)
    stale = datetime.now(UTC) - timedelta(days=9)
    db_session.add_all(
        [
            AssistantTranscript(bot_id=bot.id, session_id=uuid.uuid4(), updated_at=stale)
            for _ in range(5)
        ]
    )
    await db_session.commit()
    assert await transcripts.purge(db_session, datetime.now(UTC) - timedelta(days=4)) == 5


async def test_purge_stops_after_the_batch_cap_and_the_rest_goes_on_later_calls(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(transcripts, "PURGE_BATCH", 2)
    monkeypatch.setattr(transcripts, "PURGE_MAX_BATCHES", 2)
    bot = await seed_standard(db_session)
    stale = datetime.now(UTC) - timedelta(days=9)
    db_session.add_all(
        [
            AssistantTranscript(bot_id=bot.id, session_id=uuid.uuid4(), updated_at=stale)
            for _ in range(7)
        ]
    )
    await db_session.commit()
    before = datetime.now(UTC) - timedelta(days=4)
    assert await transcripts.purge(db_session, before) == 4
    assert await transcripts.purge(db_session, before) == 3
    assert await transcripts.purge(db_session, before) == 0


async def test_purge_skips_rows_locked_by_an_upload_without_waiting(
    db_session: AsyncSession, db_engine: AsyncEngine
):
    bot = await seed_standard(db_session)
    stale = datetime.now(UTC) - timedelta(days=9)
    busy, idle = uuid.uuid4(), uuid.uuid4()
    db_session.add_all(
        [
            AssistantTranscript(bot_id=bot.id, session_id=busy, updated_at=stale),
            AssistantTranscript(bot_id=bot.id, session_id=idle, updated_at=stale),
        ]
    )
    await db_session.commit()
    async with make_session_factory(db_engine)() as holder:
        await holder.execute(
            select(AssistantTranscript.session_id)
            .where(AssistantTranscript.session_id == busy)
            .with_for_update()
        )
        before = datetime.now(UTC) - timedelta(days=4)
        deleted = await asyncio.wait_for(transcripts.purge(db_session, before), 5)
        await db_session.commit()
        assert deleted == 1
        await holder.rollback()
    assert await db_session.get(AssistantTranscript, (bot.id, busy)) is not None
    assert await transcripts.purge(db_session, before) == 1


async def test_rearming_an_existing_row_refreshes_its_updated_at(db_session: AsyncSession):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    node = await add_node(db_session)
    info, plan = await start(db_session, bot, node.id)
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=BLOB
    )
    await db_session.execute(
        text("UPDATE assistant_transcripts SET updated_at = now() - interval '9 days'")
    )
    await db_session.commit()
    _, plan2 = await start(db_session, bot, node.id)
    assert plan2.restore is True
    row = await db_session.get(
        AssistantTranscript, (bot.id, info.relay_session_id), populate_existing=True
    )
    assert row is not None and datetime.now(UTC) - row.updated_at < timedelta(minutes=5)
    assert await transcripts.purge(db_session, datetime.now(UTC) - timedelta(days=4)) == 0
