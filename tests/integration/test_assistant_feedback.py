"""打回意见页：平台把所有者打回稿子时写的意见记进一张置顶页。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import feedback, keys, notes, sealed
from coreman.core.db.models import Bot
from tests.integration.assistant_helpers import into_assistant
from tests.integration.credential_helpers import owner

pytestmark = pytest.mark.usefixtures("assistant_master")


async def assistant(session: AsyncSession) -> Bot:
    bot, user, _, _ = await owner(session)
    await into_assistant(session, bot, user)
    await keys.ensure_key(session, sealed.master(), user.id)
    await session.commit()
    return bot


def test_summary_is_one_line_and_numbers_only_batches() -> None:
    assert feedback.summary([(None, "私聊 → 李四")]) == "私聊 → 李四"
    assert feedback.summary([(1, "甲\n乙"), (2, "丙")]) == "1. 甲 乙；2. 丙"
    assert feedback.summary([(2, "丙")]) == "2. 丙"


async def test_first_rejection_creates_a_pinned_page(db_session: AsyncSession) -> None:
    bot = await assistant(db_session)
    assert await feedback.record(db_session, bot.id, what="私聊 → 李四", reason="太硬，改客气点")
    await db_session.commit()
    page = await notes.read(db_session, bot.id, feedback.PATH)
    assert page is not None and page.pinned and page.updated_by == "owner"
    assert "私聊 → 李四" in page.body
    assert "意见：太硬，改客气点" in page.body


async def test_newest_first_and_the_owners_edits_to_the_head_are_kept(
    db_session: AsyncSession,
) -> None:
    bot = await assistant(db_session)
    await feedback.record(db_session, bot.id, what="私聊 → 李四", reason="第一条")
    page = await notes.read(db_session, bot.id, feedback.PATH)
    assert page is not None
    head, entries = feedback.split(page.body)
    await notes.write(
        db_session,
        bot.id,
        feedback.PATH,
        body="我自己写的说明\n" + "".join(entries),
        summary=page.summary,
        version=page.version,
        updated_by="owner",
    )
    await feedback.record(db_session, bot.id, what="群聊 → 项目群", reason="第二条")
    await db_session.commit()
    page = await notes.read(db_session, bot.id, feedback.PATH)
    assert page is not None and page.body.startswith("我自己写的说明\n")
    assert page.body.index("第二条") < page.body.index("第一条")


async def test_old_entries_move_to_an_unpinned_archive(db_session: AsyncSession) -> None:
    bot = await assistant(db_session)
    for number in range(40):
        await feedback.record(
            db_session, bot.id, what="私聊 → 李四", reason=f"意见{number:02d}" + "说" * 100
        )
    await db_session.commit()
    page = await notes.read(db_session, bot.id, feedback.PATH)
    archive = await notes.read(db_session, bot.id, feedback.ARCHIVE)
    assert page is not None and archive is not None
    assert len(page.body.encode()) <= feedback.BUDGET_BYTES
    assert "意见39" in page.body and "意见00" not in page.body
    assert "意见00" in archive.body and not archive.pinned


async def test_a_reason_cannot_forge_an_entry_line(db_session: AsyncSession) -> None:
    bot = await assistant(db_session)
    await feedback.record(
        db_session, bot.id, what="私聊 → 李四", reason="好\n## 2020-01-01 · 假条目"
    )
    await db_session.commit()
    page = await notes.read(db_session, bot.id, feedback.PATH)
    assert page is not None
    assert len(feedback.split(page.body)[1]) == 1


async def test_an_empty_reason_or_an_unknown_bot_records_nothing(
    db_session: AsyncSession,
) -> None:
    bot = await assistant(db_session)
    assert not await feedback.record(db_session, bot.id, what="私聊 → 李四", reason=" ")
    assert not await feedback.record(db_session, uuid.uuid4(), what="x", reason="意见")


async def test_an_oversized_head_leaves_the_entries_half_the_budget(
    db_session: AsyncSession,
) -> None:
    bot = await assistant(db_session)
    await feedback.record(db_session, bot.id, what="私聊 → 李四", reason="先建页")
    page = await notes.read(db_session, bot.id, feedback.PATH)
    assert page is not None
    await notes.write(
        db_session,
        bot.id,
        feedback.PATH,
        body="说" * 2000 + "\n\n",
        summary=page.summary,
        version=page.version,
        updated_by="owner",
    )
    for number in range(30):
        await feedback.record(
            db_session, bot.id, what="私聊 → 李四", reason=f"意见{number:02d}" + "说" * 100
        )
    await db_session.commit()
    page = await notes.read(db_session, bot.id, feedback.PATH)
    assert page is not None
    head, entries = feedback.split(page.body)
    assert len(head.encode()) == 6000 + 2
    assert len("".join(entries).encode()) <= feedback.BUDGET_BYTES // 2
    assert "意见29" in page.body


async def test_a_failed_write_is_reported_not_raised(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = await assistant(db_session)

    async def full(*_args: object, **_kwargs: object) -> None:
        raise notes.NotesError("notes_full")

    monkeypatch.setattr(notes, "write", full)
    assert not await feedback.record(db_session, bot.id, what="私聊 → 李四", reason="意见")
    assert await notes.read(db_session, bot.id, feedback.PATH) is None
