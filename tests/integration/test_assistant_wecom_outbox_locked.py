"""企微出站消费者遇到已锁定的条目：跳过，不把读不出来的内容当空消息发出去。

助理记录只能是飞书（`bots` 的检查约束），正常情况下它的出站条目到不了企微网关；这里直接
让消费者认领一条助理的条目，证明即使到了也不会发。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import outbox
from coreman.core.db.session import make_session_factory
from coreman.runtime.gateway_wecom.outbox_consumer import OutboxConsumer
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant

pytestmark = pytest.mark.usefixtures("assistant_master")


class _Socket:
    state = "subscribed"

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_confirmed(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


async def test_locked_item_is_skipped_not_sent_empty(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    for key in ("1:send:a", "2:send:a"):
        assert await outbox.add(
            db_session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key=key,
            target={"chat_id": "oc_1"},
            payload={"markdown": f"答：{SECRET}"},
        )
    await db_session.commit()
    socket = _Socket()
    runner = SimpleNamespace(
        bot=bot,
        ws=socket,
        lock=asyncio.Lock(),
        factory=make_session_factory(db_engine),
        generation=1,
    )
    consumer = OutboxConsumer(runner, min_chat_interval=0.0)  # type: ignore[arg-type]
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    await consumer.consume()
    assert socket.frames == []
    assert (
        await raw(
            db_session, "SELECT array_agg(status || ':' || last_error ORDER BY id) FROM outbox"
        )
        == [f"skipped:{sealed.LOCKED_CODE}"] * 2
    )
