"""平台经审批以所有者身份发出的消息：记下编号，盯消息读到时不当作所有者亲自发言。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import sent
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.integration.assistant_helpers import raw
from tests.integration.test_assistant_approval_execute import approved, run
from tests.integration.watch_helpers import (
    assistant,
    inbox_rows,
    install,
    ms,
    primed,
    tick,
    watch_row,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
LI = {"chat_id": "oc_li", "chat_mode": "p2p"}
SENT = "SELECT message_id FROM assistant_sent WHERE bot_id = :bot"


def base_time() -> datetime:
    return utcnow().replace(microsecond=0)


def after(base: datetime, seconds: int) -> datetime:
    return base + timedelta(seconds=seconds)


async def test_an_executed_send_records_the_message_it_produced(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, _feishu, _tower = await approved(db_session, monkeypatch)
    assert await run(db_engine, row, task) == "executed"
    assert await raw(db_session, SENT, bot=row.bot_id) == "om_sent_1"
    assert await sent.ours(db_session, row.bot_id, "om_sent_1")
    assert not await sent.ours(db_session, row.bot_id, "om_2")


async def test_a_failed_send_records_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, task, feishu, _tower = await approved(db_session, monkeypatch)
    feishu.fail = "feishu_request_failed"
    assert await run(db_engine, row, task) == "failed"
    assert await raw(db_session, "SELECT count(*) FROM assistant_sent") == 0


async def test_recording_is_per_assistant_idempotent_and_forgets_old_rows(
    db_session: AsyncSession,
) -> None:
    one = await assistant(db_session, "a")
    two = await assistant(db_session, "b")
    await sent.record(db_session, one.bot.id, ["om_x", "om_x", ""])
    await sent.record(db_session, one.bot.id, ["om_x"])
    await db_session.commit()
    assert await sent.ours(db_session, one.bot.id, "om_x")
    assert not await sent.ours(db_session, two.bot.id, "om_x")
    # 盯消息早就读过去了的旧行，下次记的时候顺手清掉。
    await db_session.execute(
        text("UPDATE assistant_sent SET created_at = now() - make_interval(days => 31)")
    )
    await sent.record(db_session, one.bot.id, ["om_y"])
    await db_session.commit()
    assert not await sent.ours(db_session, one.bot.id, "om_x")
    assert await sent.ours(db_session, one.bot.id, "om_y")


async def test_a_message_the_platform_sent_as_the_owner_does_not_hold_the_chat(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = base_time()
    await primed(db_session, who, base)
    api.chats = [LI]
    api.say("oc_li", "om_1", after(base, 10))
    # 经审批由平台发出的回复：在飞书里发送人就是所有者。
    api.say("oc_li", "om_sent", after(base, 20), sender=who.open_id, text="好的，周五给。")
    await sent.record(db_session, who.bot.id, ["om_sent"])
    await db_session.commit()
    factory = make_session_factory(db_engine)
    await tick(factory, after(base, 30))
    # 不入箱，先到的那条不被压着，「所有者上次发言」也不动；游标照常越过它。
    [first] = await inbox_rows(db_session, who)
    assert (first.message_id, first.level, first.hold_until) == ("om_1", "immediate", None)
    entry = (await watch_row(db_session, who)).cursor_enc["chats"]["oc_li"]
    assert "o" not in entry and entry["t"] == ms(after(base, 20))
    # 之后对方再发：照常分级，不按「压着」。
    api.say("oc_li", "om_3", after(base, 70))
    await tick(factory, after(base, 91))
    last = (await inbox_rows(db_session, who))[-1]
    assert (last.message_id, last.level, last.hold_until) == ("om_3", "immediate", None)
    # 所有者真的亲自发了一句：照旧压着。
    api.say("oc_li", "om_mine", after(base, 100), sender=who.open_id)
    await tick(factory, after(base, 151))
    held = {row.message_id: row.level for row in await inbox_rows(db_session, who)}
    assert held == {"om_1": "hold", "om_3": "hold"}
    entry = (await watch_row(db_session, who)).cursor_enc["chats"]["oc_li"]
    assert entry["o"] == ms(after(base, 100))
