"""托管范围：不托管的会话不读、不入箱，划出去时待交回的条目作废；只托管名单时只看名单里的会话。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import settings, watch
from coreman.core.db.session import make_session_factory
from coreman.core.feishu_personal.errors import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.timeutils import utcnow
from tests.integration.lifecycle_helpers import CIPHER
from tests.integration.watch_helpers import (
    Watched,
    assistant,
    inbox_rows,
    install,
    primed,
    tick,
    watch_row,
)

pytestmark = pytest.mark.usefixtures("assistant_master")
TEAM = {"chat_id": "oc_team", "name": "项目群", "chat_mode": "group"}
LI = {"chat_id": "oc_li", "chat_mode": "p2p"}


def ago(base: datetime, **kw: Any) -> datetime:
    return base - timedelta(**kw)


async def ready(session: AsyncSession, who: Watched, base: datetime) -> None:
    await primed(session, who, ago(base, days=3), head=["oc_team", "oc_li"])
    row = await watch_row(session, who)
    row.last_ok_at, row.next_poll_at = ago(base, seconds=60), ago(base, seconds=60)
    await session.commit()


async def by_status(session: AsyncSession, who: Watched) -> dict[str, tuple[str, str | None]]:
    return {
        str(row.message_id): (row.status, row.void_code) for row in await inbox_rows(session, who)
    }


async def test_listed_scope_reads_only_the_listed_chats(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = utcnow().replace(microsecond=0)
    await ready(db_session, who, base)
    await settings.update(
        db_session,
        who.bot.id,
        now=ago(base, hours=1),
        chat_scope="listed",
        managed_chats=["oc_team"],
    )
    await db_session.commit()
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_li", ago(base, seconds=30))
    api.say("oc_team", "om_team", ago(base, seconds=20))
    await tick(make_session_factory(db_engine), base)
    assert list(await by_status(db_session, who)) == ["om_team"]
    assert api.read("oc_li") == []
    # 会话顺序里也没有它：入口把会话列表里不托管的会话滤掉了。
    assert (await watch_row(db_session, who)).head_enc == ["oc_team"]


async def test_unmanaging_a_chat_voids_what_is_waiting_from_it(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = utcnow().replace(microsecond=0)
    await ready(db_session, who, base)
    # 只跑盯消息、不跑投递：条目留在收件箱里等交回。
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_li", ago(base, seconds=30))
    api.say("oc_team", "om_team", ago(base, seconds=20))
    await tick(make_session_factory(db_engine), base)
    assert set(await by_status(db_session, who)) == {"om_li", "om_team"}
    await settings.update(db_session, who.bot.id, now=base, ignored_chats=["oc_li"])
    await db_session.commit()
    assert await by_status(db_session, who) == {
        "om_li": ("void", "unmanaged"),
        "om_team": ("pending", None),
    }


async def test_going_back_to_all_chats_does_not_backfill_the_unmanaged_period(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = utcnow().replace(microsecond=0)
    await ready(db_session, who, base)
    await settings.update(
        db_session,
        who.bot.id,
        now=ago(base, hours=2),
        chat_scope="listed",
        managed_chats=["oc_team"],
    )
    await db_session.commit()
    api.say("oc_li", "om_while_unmanaged", ago(base, hours=1))
    await settings.update(db_session, who.bot.id, now=ago(base, minutes=5), chat_scope="all")
    await db_session.commit()
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_after", ago(base, minutes=1))
    await tick(make_session_factory(db_engine), base)
    assert list(await by_status(db_session, who)) == ["om_after"]


async def test_the_assistants_own_tools_cannot_reach_an_unmanaged_chat(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = install(monkeypatch)
    who = await assistant(db_session)
    base = utcnow().replace(microsecond=0)
    api.chats = [LI, TEAM]
    api.say("oc_li", "om_li", ago(base, minutes=3))
    api.say("oc_team", "om_team", ago(base, minutes=2))
    await settings.update(db_session, who.bot.id, now=base, ignored_chats=["oc_li"])
    prefs = await settings.get(db_session, who.bot.id)
    scope, _code = await watch.owner_scope(db_session, who.bot, prefs)
    assert scope is not None
    call = Call(db_session, CIPHER, scope)
    with pytest.raises(PersonalError) as caught:
        await call("im.history", params={"container_id_type": "chat", "container_id": "oc_li"})
    assert caught.value.code == "chat_not_managed"
    assert api.read("oc_li") == []
    found = await call("im.mget", params={"message_ids": ["om_li", "om_team"]})
    assert [item["message_id"] for item in found["items"]] == ["om_team"]
    listed = await call("im.chats", params={})
    assert [item["chat_id"] for item in listed["items"]] == ["oc_team"]
