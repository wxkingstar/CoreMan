"""待跟进的增改：编号、密文、按助理隔离、并发不重号、未结上限。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import followups, keys, sealed
from coreman.core.crypto import DecryptError
from coreman.core.db.session import make_session_factory
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
TODAY = date(2026, 11, 13)
STORED = (
    "SELECT string_agg(title_enc || coalesce(notes_enc, ''), ' ') FROM assistant_followups"
    " WHERE bot_id = :bot"
)


async def _assistant(session: AsyncSession) -> uuid.UUID:
    """一位开通好、已有数据密钥的助理。"""
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    await session.commit()
    return bot.id


async def _code(awaitable: object) -> str:
    with pytest.raises(followups.FollowupError) as caught:
        await awaitable  # type: ignore[misc]
    return caught.value.code


def test_labels() -> None:
    assert followups.label(12) == "F012" and followups.label(1234) == "F1234"
    assert [followups.parse_label(v) for v in ("F012", "f12", " 12 ")] == [12, 12, 12]
    for bad in ("F0", "X12", "", "F-1"):
        with pytest.raises(followups.FollowupError):
            followups.parse_label(bad)
    assert followups.MAX_OPEN == 500


async def test_add_numbers_items_and_stores_ciphertext(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    first = await followups.add(
        db_session,
        bot_id,
        category="promised",
        title=f"  周四前给李四报价 {SECRET}  ",
        notes=f"他周一问的。{SECRET}",
        due_on=date(2026, 11, 19),
        origin_message_id="om_1",
    )
    second = await followups.add(db_session, bot_id, category="decide", title="是否续约")
    await db_session.commit()
    assert (first.seq, second.seq) == (1, 2)
    assert first.status == "open" and first.title_enc == f"周四前给李四报价 {SECRET}"
    stored = str(await raw(db_session, STORED, bot=bot_id))
    assert SECRET not in stored and "李四" not in stored and "续约" not in stored
    assert stored.count("enc:v2:d1:") == 3
    assert followups.view(first) == {
        "id": "F001",
        "category": "promised",
        "status": "open",
        "title": f"周四前给李四报价 {SECRET}",
        "review_on": None,
        "due_on": "2026-11-19",
        "origin_message_id": "om_1",
        "created_at": first.created_at.isoformat(),
        "notes": f"他周一问的。{SECRET}",
    }
    assert "notes" not in followups.view(first, notes=False)
    assert followups.view(first, notes=False)["has_notes"] is True


async def test_add_rejects_bad_input(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    assert await _code(followups.add(db_session, bot_id, category="todo", title="x")) == (
        "invalid_category"
    )
    for title in ("   ", "长" * (followups.MAX_TITLE + 1)):
        assert await _code(followups.add(db_session, bot_id, category="remind", title=title)) == (
            "invalid_title"
        )
    too_long = "备" * (followups.MAX_NOTES + 1)
    assert (
        await _code(followups.add(db_session, bot_id, category="remind", title="x", notes=too_long))
        == "invalid_notes"
    )


async def test_update_snooze_and_close(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    await followups.add(db_session, bot_id, category="waiting", title="等王五的合同")
    row = await followups.update(
        db_session,
        bot_id,
        1,
        category="promised",
        title=f"合同回传 {SECRET}",
        notes="已催过一次",
        due_on=date(2026, 11, 20),
    )
    await db_session.commit()
    assert (row.category, row.title_enc, row.notes_enc) == (
        "promised",
        f"合同回传 {SECRET}",
        "已催过一次",
    )
    assert row.due_on == date(2026, 11, 20) and row.review_on is None
    assert SECRET not in str(await raw(db_session, STORED, bot=bot_id))
    # 没给的项不动；给 None 才清空。
    row = await followups.update(db_session, bot_id, 1, notes=None)
    assert row.notes_enc is None and row.title_enc == f"合同回传 {SECRET}"
    assert row.due_on == date(2026, 11, 20)

    # 推后必须是今天之后的某一天。
    assert await _code(followups.snooze(db_session, bot_id, 1, TODAY, today=TODAY)) == (
        "snooze_not_future"
    )
    row = await followups.snooze(db_session, bot_id, 1, date(2026, 11, 16), today=TODAY)
    assert (row.status, row.review_on) == ("snoozed", date(2026, 11, 16))
    # 清掉推后条目的回看日期：它不会自己醒，所以直接改回未结。
    row = await followups.update(db_session, bot_id, 1, review_on=None)
    assert (row.status, row.review_on) == ("open", None)

    row = await followups.close(db_session, bot_id, 1, notes="合同已收到")
    await db_session.commit()
    assert row.status == "done" and row.closed_at is not None and row.notes_enc == "合同已收到"
    assert row.close_code is None
    # 关了的不能再改、再推后；再关一次原样返回。
    assert await _code(followups.update(db_session, bot_id, 1, title="x")) == "followup_closed"
    assert (
        await _code(followups.snooze(db_session, bot_id, 1, date(2026, 11, 16), today=TODAY))
        == "followup_closed"
    )
    again = await followups.close(db_session, bot_id, 1, dropped=True)
    assert again.status == "done"

    await followups.add(db_session, bot_id, category="remind", title="不用了的事")
    dropped = await followups.close(db_session, bot_id, 2, dropped=True)
    assert dropped.status == "dropped"
    assert await _code(followups.get(db_session, bot_id, 9)) == "followup_not_found"


async def test_another_assistant_cannot_see_or_change_it(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    mine = await _assistant(db_session)
    theirs = await _assistant(db_session)
    await followups.add(db_session, mine, category="decide", title=f"我的事 {SECRET}")
    await db_session.commit()
    # 对方没有 F001：读、改、推后、关闭都找不到。
    for attempt in (
        followups.get(db_session, theirs, 1),
        followups.update(db_session, theirs, 1, title="改掉"),
        followups.snooze(db_session, theirs, 1, date(2026, 11, 16), today=TODAY),
        followups.close(db_session, theirs, 1),
    ):
        assert await _code(attempt) == "followup_not_found"
    untouched = await followups.get(db_session, mine, 1)
    assert untouched.status == "open" and untouched.title_enc == f"我的事 {SECRET}"
    # 对方的编号从自己的 1 开始。
    own = await followups.add(db_session, theirs, category="remind", title="对方的事")
    await db_session.commit()
    assert own.seq == 1
    # 把我的密文挪到对方的行上也解不开：密钥按人，密文绑定助理。
    await db_session.execute(
        text(
            "UPDATE assistant_followups SET title_enc ="
            " (SELECT title_enc FROM assistant_followups WHERE bot_id = :mine)"
            " WHERE bot_id = :theirs"
        ),
        {"mine": mine, "theirs": theirs},
    )
    await db_session.commit()
    async with make_session_factory(db_engine)() as other:
        with pytest.raises(DecryptError):
            await followups.get(other, theirs, 1)


async def test_concurrent_adds_never_share_a_number(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot_id = await _assistant(db_session)
    factory = make_session_factory(db_engine)

    async def one(index: int) -> int:
        async with factory() as session:
            row = await followups.add(session, bot_id, category="remind", title=f"第 {index} 件")
            # 拿到编号后不马上提交：没有锁的话别的事务会在这时取到同一个编号。
            await asyncio.sleep(0.05)
            await session.commit()
            return row.seq

    seqs = await asyncio.gather(*(one(index) for index in range(6)))
    assert sorted(seqs) == [1, 2, 3, 4, 5, 6]


async def test_open_items_are_capped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(followups, "MAX_OPEN", 3)
    bot_id = await _assistant(db_session)
    for index in range(2):
        await followups.add(db_session, bot_id, category="remind", title=f"第 {index} 件")
    await followups.add(db_session, bot_id, category="waiting", title="第三件")
    # 推后的也算未结。
    await followups.snooze(db_session, bot_id, 3, date(2026, 11, 16), today=TODAY)
    assert await _code(followups.add(db_session, bot_id, category="remind", title="第四件")) == (
        "too_many_followups"
    )
    # 关掉一条才有名额；编号接着往下走，不回收。
    await followups.close(db_session, bot_id, 1)
    row = await followups.add(db_session, bot_id, category="remind", title="第四件")
    assert row.seq == 4


async def test_without_a_data_key_content_is_refused_not_dropped(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """经 ORM 写入不会签发数据密钥：取不到密钥时明确报错，不能悄悄存下一条空标题。"""
    bot, _ = await seed_assistant(db_session)
    await db_session.commit()
    assert await _code(followups.add(db_session, bot.id, category="remind", title="续费")) == (
        "content_locked"
    )
    count = "SELECT count(*) FROM assistant_followups WHERE bot_id = :bot"
    assert await raw(db_session, count, bot=bot.id) == 0


async def test_after_the_key_is_destroyed_only_metadata_changes(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    await followups.add(db_session, bot.id, category="waiting", title="等合同", notes="催过")
    await followups.add(db_session, bot.id, category="remind", title="续费")
    await db_session.commit()
    async with make_session_factory(db_engine)() as other:
        await keys.destroy_key(other, owner.id)
        await other.commit()
        locked = await followups.get(other, bot.id, 1)
        assert sealed.is_locked(locked) and followups.view(locked)["locked"] is True
        assert followups.view(locked)["title"] == ""
        for attempt in (
            followups.add(other, bot.id, category="remind", title="新的"),
            followups.update(other, bot.id, 1, title="改标题"),
            followups.update(other, bot.id, 1, notes="改备注"),
            followups.close(other, bot.id, 1, notes="怎么了结的"),
        ):
            assert await _code(attempt) == "content_locked"
        # 只动元数据的照常：改期、推后、关闭。
        row = await followups.update(other, bot.id, 1, due_on=date(2026, 11, 20))
        assert row.due_on == date(2026, 11, 20)
        row = await followups.snooze(other, bot.id, 2, date(2026, 11, 16), today=TODAY)
        assert row.status == "snoozed"
        row = await followups.close(other, bot.id, 1)
        await other.commit()
        assert row.status == "done"
    # 库里的密文没有被空串盖掉。
    assert str(await raw(db_session, STORED, bot=bot.id)).count("enc:v2:d1:") == 3
