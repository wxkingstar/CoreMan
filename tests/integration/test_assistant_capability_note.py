"""开轮：能力清单进本轮块、不进稳定段；授权给助理的系统让对应缺口自动解决。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import capabilities, followups, gaps
from coreman.core.db.models import BotSystemGrant, BusinessSystem
from tests.integration.assistant_helpers import identified
from tests.integration.lifecycle_helpers import active_assistant, install
from tests.integration.shift_helpers import say
from tests.integration.worker_helpers import turn_block

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


@pytest.fixture(autouse=True)
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)


async def test_note_in_turn_block_and_gap_resolved(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    db_session.add(BusinessSystem(key="orders", name="订单系统", allowed_bot_ids=None))
    await db_session.flush()
    db_session.add(BotSystemGrant(bot_id=bot.id, system_key="orders"))
    await followups.add(db_session, bot.id, category="promised", title="查订单")
    await gaps.report(
        db_session, bot.id, kind="business_system", key="orders", need="查订单", followup_seqs=[1]
    )
    await db_session.commit()
    done, relay = await say(db_engine, db_session, bot, "你好")
    assert done.status == "succeeded" and len(relay.requests) == 1
    block = turn_block(relay.requests[0])
    assert capabilities.HEAD in block and "订单系统（orders）" in block
    assert capabilities.HEAD not in relay.requests[0]["messages"][0]["content"]
    assert (await gaps.get(db_session, bot.id, 1)).status == "resolved"
