"""outbox：出站载荷加密入库，网关发送前取到明文；内部键与卡片的 task_id 仍可被 SQL 过滤。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import app_leases, instances, outbox
from coreman.core.db.models import Bot, OutboxItem
from coreman.core.db.session import make_session_factory
from coreman.runtime.gateway_feishu.app_outbound import AppOutbound
from coreman.runtime.gateway_feishu.transport import FeishuTransport
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
PAYLOAD = {
    "markdown": SECRET,
    "card": {"task_id": "choice:1:q0", "body": SECRET},
    "_reply": True,
}


async def _add(session: AsyncSession, bot_id, key: str = "77:send:final") -> OutboxItem:  # type: ignore[no-untyped-def]
    item = await outbox.add(
        session,
        bot_id=bot_id,
        platform="feishu",
        kind="send",
        dedupe_key=key,
        target={"chat_id": "oc_1"},
        payload=PAYLOAD,
    )
    await session.commit()
    assert item is not None
    return item


async def _stored(session: AsyncSession, item_id: int) -> str:
    return str(
        await raw(
            session,
            "SELECT payload::text || '|' || target::text FROM outbox WHERE id = :id",
            id=item_id,
        )
    )


async def test_payload_roundtrip_and_sql_filters(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    item = await _add(db_session, bot.id)
    assert item.payload == PAYLOAD
    stored = await _stored(db_session, item.id)
    assert SECRET not in stored and '"chat_id": "oc_1"' in stored
    envelope = json.loads(stored.split("|")[0])
    assert envelope["_reply"] is True and envelope["card"] == {"task_id": "choice:1:q0"}
    async with make_session_factory(db_engine)() as other:
        claimed = await outbox.claim_next(other, bot_id=bot.id)
        assert claimed is not None and claimed.payload == PAYLOAD
        # 网关发出后记下平台消息编号：内部键留明文，其余重新封存。
        claimed.payload = {**claimed.payload, "_feishu_message_id": "om_9"}
        await outbox.mark_sent(other, claimed.id)
        await other.commit()
    assert SECRET not in await _stored(db_session, item.id)
    found = await db_session.scalar(
        select(OutboxItem.id).where(
            OutboxItem.bot_id == bot.id,
            OutboxItem.status == "sent",
            OutboxItem.payload["_feishu_message_id"].astext == "om_9",
            OutboxItem.payload["card"]["task_id"].astext == "choice:1:q0",
        )
    )
    assert found == item.id
    again = await db_session.get(OutboxItem, item.id, populate_existing=True)
    assert again is not None and again.payload == {**PAYLOAD, "_feishu_message_id": "om_9"}


async def test_destroyed_key_marks_the_item_locked(db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    await _add(db_session, bot.id)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    claimed = await outbox.claim_next(db_session, bot_id=bot.id)
    assert claimed is not None and sealed.is_locked(claimed)
    assert "markdown" not in claimed.payload and claimed.payload["_reply"] is True
    await outbox.mark_skipped(db_session, claimed.id, sealed.LOCKED_CODE)
    await db_session.commit()
    assert await outbox.claim_next(db_session, bot_id=bot.id) is None


async def test_standard_bot_outbox_stays_plain(db_session: AsyncSession) -> None:
    bot = await seed_standard(db_session)
    item = await _add(db_session, bot.id)
    stored = await _stored(db_session, item.id)
    assert SECRET in stored and "_sealed" not in stored


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs.get("json") or {}))
        return {"data": {"message_id": "om_sent"}}


async def _transport(
    session: AsyncSession, db_engine: AsyncEngine, bot: Bot
) -> tuple[FeishuTransport, FakeAPI]:
    await instances.register(
        session, instance_id="gw", service="gateway-feishu", version="test", capacity=None
    )
    # 助理记录的连接按应用认领，没有自己的 bot_leases 行：围栏是应用的租约。
    assert bot.platform_app_id is not None
    await app_leases.ensure_rows(session)
    generation = await app_leases.acquire(session, bot.platform_app_id, "gw")
    assert generation is not None
    await session.commit()
    api = FakeAPI()
    outbound = AppOutbound(
        make_session_factory(db_engine),
        api,  # type: ignore[arg-type]
        app=bot.platform_app_id,
        instance_id="gw",
        generation=generation,
    )
    return outbound.transport(bot.id), api


async def _send_item(session: AsyncSession, bot: Bot) -> int:
    item = await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key="88:send:final",
        target={"chat_id": "oc_1", "message_id": "om_in"},
        payload={"text": SECRET, "_reply": True},
    )
    await session.commit()
    assert item is not None
    return item.id


async def test_gateway_sends_plaintext_and_reseals_what_it_records(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("coreman.runtime.gateway_feishu.transport.CALL_SPACING_SECONDS", 0)
    bot, _ = await seed_assistant(db_session)
    transport, api = await _transport(db_session, db_engine, bot)
    item_id = await _send_item(db_session, bot)
    assert await transport.consume_one() is True
    # 发给平台的是明文；库里的载荷仍是密文，网关记下的消息编号留在明文部分。
    assert SECRET in json.dumps(api.calls, ensure_ascii=False)
    stored = await _stored(db_session, item_id)
    assert SECRET not in stored and '"_feishu_message_id": "om_sent"' in stored
    assert await raw(db_session, "SELECT status FROM outbox WHERE id = :id", id=item_id) == "sent"


async def test_gateway_skips_an_item_whose_key_is_destroyed(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    transport, api = await _transport(db_session, db_engine, bot)
    item_id = await _send_item(db_session, bot)
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    assert await transport.consume_one() is True
    assert api.calls == []
    assert (
        await raw(
            db_session,
            "SELECT status || '|' || last_error FROM outbox WHERE id = :id",
            id=item_id,
        )
        == "skipped|content_locked"
    )
