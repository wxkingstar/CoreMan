"""换运行时后的首轮排队超时：交接编号作废之后，下一轮照样带上平台保存的上下文说明。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed, transcripts
from coreman.core.chat import sessions
from coreman.core.db.models import AssistantTranscript
from tests.fakes.runtime_node import add_node
from tests.integration.assistant_helpers import seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")
KEY = "oc_owner"


async def turn(session: AsyncSession, bot, node_id):
    info = await sessions.get_or_create(
        session,
        bot_id=bot.id,
        session_key=KEY,
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    out = await transcripts.prepare(
        session,
        bot=bot,
        node_id=node_id,
        info=info,
        session_key=KEY,
        backend="claude",
        speaker_user_id=None,
    )
    await session.commit()
    return out


async def switched(session: AsyncSession):
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    node = await add_node(session)
    first, plan = await turn(session, bot, node.id)
    await transcripts.upload(
        session, node_id=node.id, transfer=plan.transfer, status="ok", data=b"\x1f\x8bold"
    )
    await sessions.carry_over(session, bot.id, backend="claude", keep=set())
    await session.commit()
    return bot, node, first


async def test_a_released_first_turn_keeps_the_carried_context(db_session: AsyncSession):
    bot, node, first = await switched(db_session)
    info, plan = await turn(db_session, bot, node.id)
    assert info.inherited_from == first.relay_session_id and plan.restore is False
    # 排队超时、确定没有节点领到：编号作废，行留下（没有内容）。
    assert await transcripts.release(
        db_session, bot_id=bot.id, session_id=info.relay_session_id, transfer=plan.transfer
    )
    await db_session.commit()
    again, plan2 = await turn(db_session, bot, node.id)
    assert again.relay_session_id == info.relay_session_id
    assert again.inherited_from == first.relay_session_id
    assert plan2.restore is False


async def test_an_unreturned_first_turn_still_starts_over(db_session: AsyncSession):
    """首轮派出了、转录没传回来（编号还在）：照旧不续、换新会话，也不再带续接。"""
    bot, node, _ = await switched(db_session)
    info, _plan = await turn(db_session, bot, node.id)
    after, plan2 = await turn(db_session, bot, node.id)
    assert after.relay_session_id != info.relay_session_id and after.inherited_from is None
    assert plan2.restore is False


async def test_a_stored_transcript_is_restored_not_carried(db_session: AsyncSession):
    bot, node, _ = await switched(db_session)
    info, plan = await turn(db_session, bot, node.id)
    await transcripts.upload(
        db_session, node_id=node.id, transfer=plan.transfer, status="ok", data=b"\x1f\x8bnew"
    )
    await db_session.commit()
    again, plan2 = await turn(db_session, bot, node.id)
    assert again.relay_session_id == info.relay_session_id and plan2.restore is True
    row = await db_session.get(AssistantTranscript, (bot.id, info.relay_session_id))
    assert row is not None and row.pending_transfer == plan2.transfer
