"""反向通道排队：节点满载时对话排队等空位；超时归类为「繁忙」并撤销仍在排队的调用。"""

import asyncio
import base64
import time
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from coreman.core.db.models import RuntimeCall, RuntimeNode
from coreman.core.db.session import make_session_factory
from coreman.core.runtime_nodes import transport
from coreman.core.runtime_nodes.transport import (
    TOTAL_TIMEOUT_EXTENSION,
    ReverseTransport,
    RuntimeQueueTimeout,
    call_seconds,
    now,
)
from tests.api.test_runtime_nodes import enrollment, poll_command

CHAT = {TOTAL_TIMEOUT_EXTENSION: 3600.0}


def _caller(app, db_engine, node_id: uuid.UUID) -> httpx.AsyncClient:
    reverse = ReverseTransport(
        node_id, "claude", factory=make_session_factory(db_engine), cipher=app.state.cipher
    )
    # connect 故意很短：对话排队不受它约束，探测类请求才受
    return httpx.AsyncClient(
        transport=reverse, base_url="http://node/claude", timeout=httpx.Timeout(10, connect=0.3)
    )


async def _calls(db_engine, node_id: uuid.UUID) -> list[RuntimeCall]:
    async with make_session_factory(db_engine)() as session:
        return list(
            await session.scalars(select(RuntimeCall).where(RuntimeCall.node_id == node_id))
        )


def test_call_deadline_follows_the_declared_total_timeout() -> None:
    margin = transport.QUEUE_WAIT_SECONDS + transport.CALL_DEADLINE_MARGIN_SECONDS
    assert call_seconds(3600) == 3600 + margin
    # bots.sse_timeout_seconds 上限 12 小时：超过 2 小时的任务不再被期限截断
    assert call_seconds(43200) == 43200 + margin
    assert call_seconds(10**9) == 43200 + margin
    for undeclared in (None, 0, -1, True, "3600"):
        assert call_seconds(undeclared) == transport.DEFAULT_CALL_SECONDS


async def test_chat_waits_in_queue_beyond_connect_timeout_until_a_slot_frees(
    client, db_session, app, db_engine
):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    async with _caller(app, db_engine, node_id) as caller:
        pending = asyncio.create_task(caller.post("/v1/chat/completions", json={}, extensions=CHAT))
        await asyncio.sleep(1.0)  # 远超 connect 0.3 秒：节点满载、一直没来领
        assert not pending.done()
        (row,) = await _calls(db_engine, node_id)
        assert row.status == "queued"
        expected = call_seconds(3600.0)
        assert abs((row.deadline - now()).total_seconds() - expected) < 30
        command = await poll_command(client, headers)
        frame = {
            "seq": 0,
            "data": base64.b64encode(b"ok").decode(),
            "status_code": 200,
            "done": True,
        }
        endpoint = f"/api/runtime/calls/{command['id']}/frames"
        assert (await client.post(endpoint, headers=headers, json=frame)).status_code == 200
        assert (await asyncio.wait_for(pending, 5)).text == "ok"


async def test_queue_timeout_is_busy_and_cancels_the_queued_call(
    client, db_session, app, db_engine, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(transport, "QUEUE_WAIT_SECONDS", 0.8)
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    async with _caller(app, db_engine, node_id) as caller:
        started = time.monotonic()
        with pytest.raises(RuntimeQueueTimeout, match="排队"):
            await caller.post("/v1/chat/completions", json={}, extensions=CHAT)
        assert 0.7 <= time.monotonic() - started < 5
    (row,) = await _calls(db_engine, node_id)
    assert row.status == "cancelled" and row.request_enc == ""
    # 调用方已经放弃：节点空出来后也领不到它，不会出现「没人等结果却在执行」
    polled = await client.post("/api/runtime/poll", headers=headers, json={"slots": 4})
    assert polled.json()["data"]["commands"] == []


async def test_probe_without_total_timeout_still_fails_fast_on_connect(
    client, db_session, app, db_engine
):
    _, body, _ = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    async with _caller(app, db_engine, node_id) as caller:
        started = time.monotonic()
        with pytest.raises(httpx.ConnectTimeout) as caught:
            await caller.get("/health")
        assert not isinstance(caught.value, RuntimeQueueTimeout)
        assert time.monotonic() - started < 3
    (row,) = await _calls(db_engine, node_id)
    assert row.status == "cancelled"
    assert abs((row.deadline - now()).total_seconds() - transport.DEFAULT_CALL_SECONDS) < 30


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"heartbeat_at": None}, "离线"),
        ({"draining": True}, "排空"),
    ],
    ids=["offline", "draining"],
)
async def test_queued_chat_gives_up_early_when_the_node_cannot_take_it(
    client, db_session, app, db_engine, monkeypatch: pytest.MonkeyPatch, change, reason
):
    monkeypatch.setattr(transport, "NODE_CHECK_SECONDS", 0.2)
    _, body, _ = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    async with _caller(app, db_engine, node_id) as caller:
        pending = asyncio.create_task(caller.post("/v1/chat/completions", json={}, extensions=CHAT))
        await asyncio.sleep(0.4)
        values = dict(change)
        if "heartbeat_at" in values:
            values["heartbeat_at"] = now() - timedelta(minutes=5)
        async with make_session_factory(db_engine)() as session:
            await session.execute(
                update(RuntimeNode).where(RuntimeNode.id == node_id).values(**values)
            )
            await session.commit()
        with pytest.raises(httpx.ConnectError, match=reason):
            await asyncio.wait_for(pending, 5)
    (row,) = await _calls(db_engine, node_id)
    assert row.status == "cancelled"
