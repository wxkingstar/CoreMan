"""私聊「看板」：只有所有者能调出，平台直接回卡片，不经模型、不开轮次。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import followups
from coreman.core.db.models import OutboxItem
from tests.integration.assistant_helpers import identified
from tests.integration.lifecycle_helpers import active_assistant, install
from tests.integration.shift_helpers import last_sent, say

pytestmark = pytest.mark.usefixtures("assistant_master", "runtime_settings")


@pytest.fixture(autouse=True)
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)


async def test_owner_gets_the_board_without_the_model(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    await followups.add(db_session, bot.id, category="decide", title="选供应商", notes="建议：选 A")
    await followups.add(db_session, bot.id, category="waiting", title="等赵六回复")
    await db_session.commit()
    done, relay = await say(db_engine, db_session, bot, "看板")
    assert done.status == "succeeded" and done.result == {"assistant": "board_sent"}
    assert relay.requests == []
    sent = await last_sent(db_session, bot)
    assert sent is not None and sent.dedupe_key.startswith(f"assistant-digest:{bot.id}:board-")
    assert sent.target == {"chat_id": "oc_private"}
    body = json.dumps(sent.payload["card"], ensure_ascii=False)
    assert "F001 选供应商" in body and "建议：选 A" in body and "等赵六回复" in body
    assert "事项看板" in body
    assert sent.payload["_digest_rows"] == [1]


async def test_someone_else_gets_nothing(db_engine: AsyncEngine, db_session: AsyncSession) -> None:
    bot, _, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    done, relay = await say(db_engine, db_session, bot, "看板", sender="stranger_pid")
    assert done.result == {"assistant": "board_not_owner"} and relay.requests == []
    assert await last_sent(db_session, bot) is None


async def test_board_word_is_not_merged_into_a_running_turn() -> None:
    from coreman.runtime.worker import assistant_board

    assert "看板" in assistant_board.COMMANDS


async def test_closing_an_item_greys_out_its_button_on_the_board(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _, _ = await active_assistant(db_session)
    await identified(db_session, bot)
    row = await followups.add(db_session, bot.id, category="decide", title="选供应商")
    await db_session.commit()
    await say(db_engine, db_session, bot, "看板")
    sent = await last_sent(db_session, bot)
    assert sent is not None
    # 网关发出之后记下飞书的消息编号。
    sent.status = "sent"
    sent.payload = {**sent.payload, "_feishu_message_id": "om_board"}
    await db_session.commit()
    await followups.close(db_session, bot.id, row.seq)
    await db_session.commit()
    updates = list(
        await db_session.scalars(
            select(OutboxItem).where(OutboxItem.bot_id == bot.id, OutboxItem.kind == "card_update")
        )
    )
    assert len(updates) == 1
    assert updates[0].target == {"message_id": "om_board", "chat_id": "oc_private"}
