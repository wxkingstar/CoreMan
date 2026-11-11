"""feishu_deliveries：流式卡片的单元布局（思考预览、各段正文、任务清单）加密入库。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import instances, leases, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.db.models import Bot, BotLease, FeishuDelivery, TaskStream
from coreman.core.db.session import make_session_factory
from coreman.runtime.gateway_feishu.transport import FeishuTransport
from tests.integration.assistant_helpers import SECRET, raw, seed_assistant, seed_standard

pytestmark = pytest.mark.usefixtures("assistant_master")
LAYOUT = "SELECT layout::text FROM feishu_deliveries WHERE task_id = :id"
PLAN = [{"text": f"清单：{SECRET}", "status": "pending"}]


@pytest.fixture(autouse=True)
def _no_call_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coreman.runtime.gateway_feishu.transport.CALL_SPACING_SECONDS", 0)


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.messages: dict[str, str] = {}

    async def call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        body = kwargs.get("json") or {}
        self.calls.append((method, path, body))
        if path == "/open-apis/cardkit/v1/cards":
            return {"data": {"card_id": "card1"}}
        if path.endswith("/reply") or path == "/open-apis/im/v1/messages":
            mid = self.messages.setdefault(body["uuid"], f"om_{len(self.messages) + 1}")
            return {"data": {"message_id": mid}}
        return {"data": {}}


async def _streaming(session: AsyncSession, bot: Bot) -> tuple[int, int]:
    await instances.register(
        session, instance_id="gw", service="gateway-feishu", version="test", capacity=None
    )
    # 助理记录的连接按应用认领，没有自己的 bot_leases 行；这里只测内容加密，直接放一行当围栏。
    session.add(BotLease(bot_id=bot.id, platform="feishu"))
    await session.flush()
    lease = await leases.acquire(session, bot_id=bot.id, platform="feishu", instance_id="gw")
    assert lease is not None
    task = await tasks.enqueue(session, NewTask(bot_id=bot.id, kind="chat", payload={}))
    assert task is not None
    await streams.create(
        session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s",
        reply_context={"chat_id": "oc_1", "message_id": "om_in"},
        lease_generation=lease.generation,
        running_since=datetime.now(UTC),
    )
    await streams.update(
        session,
        task.id,
        bot_id=bot.id,
        thinking_md=f"想：{SECRET}",
        pending_text=f"第一段：{SECRET}",
        plan=PLAN,
    )
    await session.commit()
    return task.id, lease.generation


async def test_assistant_layout_is_sealed_and_gateway_keeps_pushing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    task_id, generation = await _streaming(db_session, bot)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="gw", generation=generation
    )
    await transport.round()
    stored = str(await raw(db_session, LAYOUT, id=task_id))
    assert SECRET not in stored and "enc:v2:d1:" in stored
    # 发给飞书的仍是明文。
    assert any(SECRET in str(body) for _, _, body in api.calls)
    async with factory() as other:
        delivery = await other.get(FeishuDelivery, task_id)
        assert delivery is not None and delivery.bot_id == bot.id
        assert SECRET in str(delivery.layout["thinking"])
        assert delivery.layout["plan"] == PLAN and delivery.layout["units"]
        # 接着写一段：网关在上一轮记下的布局上增量更新。
        await streams.update(
            other, task_id, bot_id=bot.id, pending_text=f"第一段：{SECRET}\n\n第二段内容"
        )
        await other.commit()
    before = len(api.calls)
    await transport.round()
    assert any("第二段内容" in str(body) for _, _, body in api.calls[before:])
    assert SECRET not in str(await raw(db_session, LAYOUT, id=task_id))
    async with factory() as other:
        await streams.complete(other, task_id, final_text=f"终稿：{SECRET}", bot_id=bot.id)
        await other.commit()
    await transport.round()
    assert SECRET not in str(await raw(db_session, LAYOUT, id=task_id))
    async with factory() as other:
        row = await other.get(TaskStream, task_id)
        assert row is not None and row.finish_pushed_at is not None
        done = await other.get(FeishuDelivery, task_id)
        assert done is not None and done.is_static


async def test_destroyed_key_blanks_the_layout_and_stops_pushing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    task_id, generation = await _streaming(db_session, bot)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="gw", generation=generation
    )
    await transport.round()
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    async with factory() as other:
        delivery = await other.get(FeishuDelivery, task_id)
        assert delivery is not None and sealed.is_locked(delivery)
        assert "thinking" not in delivery.layout and "units" not in delivery.layout
    before = len(api.calls)
    await transport.round()
    assert not any(SECRET in str(body) for _, _, body in api.calls[before:])


async def test_standard_bot_layout_stays_plain(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    bot = await seed_standard(db_session)
    task_id, generation = await _streaming(db_session, bot)
    factory, api = make_session_factory(db_engine), FakeAPI()
    transport = FeishuTransport(
        factory, api, bot_id=bot.id, instance_id="gw", generation=generation
    )
    await transport.round()
    stored = str(await raw(db_session, LAYOUT, id=task_id))
    assert SECRET in stored and "enc:v2:" not in stored and "_sealed" not in stored
