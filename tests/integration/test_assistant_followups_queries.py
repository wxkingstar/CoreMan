"""待跟进的读取：各个 scope、当天到期、推后到期后重新出现、起因消息撤回自动关闭。"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import followups, keys, sealed
from coreman.core.db.models import AssistantFollowup
from tests.integration.assistant_helpers import raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
TODAY = date(2026, 11, 13)


async def _assistant(session: AsyncSession) -> uuid.UUID:
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    await session.commit()
    return bot.id


def _seqs(rows: list[AssistantFollowup]) -> list[int]:
    return [row.seq for row in rows]


async def _seed(session: AsyncSession, bot_id: uuid.UUID) -> None:
    """F001 今天到期；F002 下周到期；F003 回看日已过；F004 没日期；F005 推到 16 号；F006 办完。"""
    add = followups.add
    await add(session, bot_id, category="decide", title="定供应商", due_on=TODAY)
    await add(session, bot_id, category="promised", title="给李四报价", due_on=date(2026, 11, 19))
    await add(session, bot_id, category="waiting", title="等合同", review_on=date(2026, 11, 12))
    await add(session, bot_id, category="remind", title="续费")
    await add(session, bot_id, category="remind", title="体检")
    await add(session, bot_id, category="decide", title="已经定了的事")
    await followups.snooze(session, bot_id, 5, date(2026, 11, 16), today=TODAY)
    await followups.close(session, bot_id, 6)
    await session.commit()


async def test_scopes(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    await _seed(db_session, bot_id)

    async def listed(scope: str, **kw: int) -> list[AssistantFollowup]:
        return await followups.list_(db_session, bot_id, scope, today=TODAY, **kw)

    today = await listed("today")
    assert _seqs(today) == [1, 3]
    # 取出来的是明文。
    assert [row.title_enc for row in today] == ["定供应商", "等合同"]
    assert _seqs(await listed("open")) == [1, 2, 3, 4, 5]
    assert [row.status for row in await listed("open")][-1] == "snoozed"
    assert _seqs(await listed("all")) == [6, 5, 4, 3, 2, 1]
    assert _seqs(await listed("decide")) == [1]
    assert _seqs(await listed("remind")) == [4, 5]
    assert _seqs(await listed("promised")) == [2]
    assert _seqs(await listed("waiting")) == [3]
    assert _seqs(await listed("open", limit=2)) == [1, 2]
    assert _seqs(await listed("all", limit=2)) == [6, 5]
    with pytest.raises(followups.FollowupError) as caught:
        await listed("everything")
    assert caught.value.code == "invalid_scope"
    assert followups.SCOPES == ("today", "open", "all", "decide", "promised", "waiting", "remind")


async def test_due_follows_the_calendar(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    await _seed(db_session, bot_id)
    assert _seqs(await followups.due(db_session, bot_id, date(2026, 11, 11))) == []
    assert _seqs(await followups.due(db_session, bot_id, date(2026, 11, 12))) == [3]
    assert _seqs(await followups.due(db_session, bot_id, TODAY)) == [1, 3]
    # 同一天再问一次还是这些：到期不是一次性的。
    assert _seqs(await followups.due(db_session, bot_id, TODAY)) == [1, 3]
    assert _seqs(await followups.due(db_session, bot_id, date(2026, 11, 19))) == [1, 2, 3, 5]


async def test_a_snoozed_item_comes_back_on_its_day(db_session: AsyncSession) -> None:
    bot_id = await _assistant(db_session)
    await _seed(db_session, bot_id)
    # 今天到期的 F001 推到明天：今天不再出现，哪怕期限就是今天。
    await followups.snooze(db_session, bot_id, 1, date(2026, 11, 14), today=TODAY)
    await db_session.commit()
    assert _seqs(await followups.due(db_session, bot_id, TODAY)) == [3]
    status = "SELECT status FROM assistant_followups WHERE bot_id = :bot AND seq = :seq"
    assert await raw(db_session, status, bot=bot_id, seq=1) == "snoozed"
    back = await followups.due(db_session, bot_id, date(2026, 11, 14))
    await db_session.commit()
    assert _seqs(back) == [1, 3] and back[0].status == "open"
    # 库里的状态也改回来了；回看日期留着。
    assert await raw(db_session, status, bot=bot_id, seq=1) == "open"
    assert back[0].review_on == date(2026, 11, 14)
    # F005 还没到 16 号。
    assert await raw(db_session, status, bot=bot_id, seq=5) == "snoozed"
    listed = await followups.list_(db_session, bot_id, "remind", today=date(2026, 11, 16))
    assert [(row.seq, row.status) for row in listed] == [(4, "open"), (5, "open")]


async def test_other_assistants_are_not_listed(db_session: AsyncSession) -> None:
    mine = await _assistant(db_session)
    theirs = await _assistant(db_session)
    await _seed(db_session, mine)
    for scope in followups.SCOPES:
        assert await followups.list_(db_session, theirs, scope, today=TODAY) == []
    assert await followups.due(db_session, theirs, TODAY) == []
    # 对方读清单（会唤醒到期的推后条目）不动我的行。
    await followups.due(db_session, theirs, date(2026, 12, 1))
    assert (await followups.get(db_session, mine, 5)).status == "snoozed"


async def test_recalled_origin_closes_its_followups(db_session: AsyncSession) -> None:
    mine = await _assistant(db_session)
    theirs = await _assistant(db_session)
    add = followups.add
    await add(db_session, mine, category="promised", title="回李四", origin_message_id="om_1")
    await add(db_session, mine, category="waiting", title="等李四", origin_message_id="om_1")
    await add(db_session, mine, category="remind", title="别的事", origin_message_id="om_2")
    await add(db_session, mine, category="decide", title="早办完了", origin_message_id="om_1")
    await add(db_session, mine, category="remind", title="没有起因")
    await followups.snooze(db_session, mine, 2, date(2026, 11, 16), today=TODAY)
    await followups.close(db_session, mine, 4)
    await add(db_session, theirs, category="remind", title="同号消息", origin_message_id="om_1")
    await db_session.commit()

    closed = await followups.close_for_message(db_session, mine, "om_1")
    await db_session.commit()
    assert _seqs(closed) == [1, 2]
    assert [row.title_enc for row in closed] == ["回李四", "等李四"]
    for row in closed:
        assert (row.status, row.close_code) == ("dropped", "origin_recalled")
        assert row.closed_at is not None
        assert followups.view(row)["close_code"] == "origin_recalled"
    # 别的消息引起的、早已办完的、没有起因的、别人的，都不动。
    assert (await followups.get(db_session, mine, 3)).status == "open"
    done = await followups.get(db_session, mine, 4)
    assert (done.status, done.close_code) == ("done", None)
    assert (await followups.get(db_session, mine, 5)).status == "open"
    assert (await followups.get(db_session, theirs, 1)).status == "open"
    # 关过了再来一次什么都不做；空的消息编号不匹配「没有起因」的条目。
    assert await followups.close_for_message(db_session, mine, "om_1") == []
    assert await followups.close_for_message(db_session, mine, "") == []
    assert _seqs(await followups.list_(db_session, mine, "open", today=TODAY)) == [3, 5]
