"""「不开 G003」由平台截下、不经模型；「开通 G003」开一轮在场轮次，本轮块里附着怎么开。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import followups, gaps
from coreman.core.db.models import Bot
from tests.integration.assistant_helpers import identified
from tests.integration.lifecycle_helpers import active_assistant, install
from tests.integration.shift_helpers import last_sent, say
from tests.integration.worker_helpers import turn_block

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


@pytest.fixture(autouse=True)
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)


async def _seeded(
    db_session: AsyncSession, kind: str = "feishu_scope", key: str = "im:message"
) -> Bot:
    bot, _, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    await followups.add(db_session, bot.id, category="promised", title="整理群里的需求")
    await gaps.report(db_session, bot.id, kind=kind, key=key, need="读群消息", followup_seqs=[1])
    await db_session.commit()
    return bot


async def test_decline_is_intercepted(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot = await _seeded(db_session)
    done, relay = await say(db_engine, db_session, bot, "不开 G001")
    assert done.result == {"assistant": "gap_declined"} and relay.requests == []
    assert (await gaps.get(db_session, bot.id, 1)).status == "declined"
    sent = await last_sent(db_session, bot)
    assert sent is not None and "G001" in sent.payload["markdown"]


async def test_decline_by_someone_else_does_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _seeded(db_session)
    done, relay = await say(db_engine, db_session, bot, "不开 G001", sender="stranger_pid")
    assert done.result == {"assistant": "board_not_owner"} and relay.requests == []
    assert (await gaps.get(db_session, bot.id, 1)).status == "open"


async def test_decline_of_an_unknown_gap(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot = await _seeded(db_session)
    done, relay = await say(db_engine, db_session, bot, "不开 G009")
    assert done.result == {"assistant": "gap_not_found"} and relay.requests == []
    sent = await last_sent(db_session, bot)
    assert sent is not None and "没有找到 G009" in sent.payload["markdown"]


async def test_open_starts_an_attended_turn(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _seeded(db_session)
    done, relay = await say(db_engine, db_session, bot, "开通 G001")
    assert done.status == "succeeded" and len(relay.requests) == 1
    block = turn_block(relay.requests[0])
    assert "要开通缺口 G001" in block and "feishu_authorize" in block and "F001" in block


async def test_open_reopens_a_declined_gap(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await _seeded(db_session)
    await gaps.decline(db_session, bot.id, 1)
    await db_session.commit()
    done, relay = await say(db_engine, db_session, bot, "开通 G001")
    assert done.status == "succeeded" and len(relay.requests) == 1
    bot_id = bot.id
    db_session.expire_all()
    assert (await gaps.get(db_session, bot_id, 1)).status == "open"
