"""节点协议 2：长轮询、通知唤醒、批量响应帧与共享连接池。"""

import asyncio
import base64
import json
import time
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from coreman.core.db.models import RuntimeCall, RuntimeNode
from coreman.core.db.session import make_session_factory
from coreman.core.runtime_nodes import transport
from coreman.core.runtime_nodes.transport import ReverseTransport, envelope_aad, now
from tests.api.test_runtime_nodes import enrollment, poll_command


def _caller(app, db_engine, node_id: uuid.UUID, provider: str = "claude") -> httpx.AsyncClient:
    reverse = ReverseTransport(
        node_id, provider, factory=make_session_factory(db_engine), cipher=app.state.cipher
    )
    return httpx.AsyncClient(transport=reverse, base_url=f"http://node/{provider}", timeout=10)


def _frame(seq: int, data: bytes = b"", **extra: object) -> dict[str, object]:
    return {"seq": seq, "data": base64.b64encode(data).decode(), **extra}


@pytest.fixture
def notifications_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底轮询拉长到 30 秒：用例里 10 秒内的唤醒只可能来自 pg_notify。"""
    monkeypatch.setattr(transport, "FALLBACK_POLL_SECONDS", 30.0)


async def test_long_poll_is_woken_by_new_call_and_reports_protocol(
    client, db_session, app, db_engine, notifications_only
):
    _, body, headers = await enrollment(client, db_session)
    started = time.monotonic()
    idle = await client.post("/api/runtime/poll", headers=headers, json={"slots": 1, "wait": 0.3})
    assert idle.status_code == 200, idle.text
    assert idle.json()["data"] == {
        "commands": [],
        "cancelled": [],
        "draining": False,
        "protocol": 2,
    }
    assert time.monotonic() - started >= 0.25
    async with _caller(app, db_engine, uuid.UUID(body["node_id"])) as caller:
        waiting = asyncio.create_task(
            client.post("/api/runtime/poll", headers=headers, json={"slots": 1, "wait": 10})
        )
        await asyncio.sleep(0.3)
        assert not waiting.done()
        requested = time.monotonic()
        pending = asyncio.create_task(caller.get("/health"))
        result = await asyncio.wait_for(waiting, 5)
        assert time.monotonic() - requested < 5
        command = result.json()["data"]["commands"][0]
        assert command["path"] == "/health"
        # 领取与写帧同样靠通知唤醒消费者，而不是 30 秒的兜底轮询。
        frame = _frame(0, b"ok", status_code=200, done=True)
        endpoint = f"/api/runtime/calls/{command['id']}/frames"
        assert (await client.post(endpoint, headers=headers, json=frame)).status_code == 200
        assert (await asyncio.wait_for(pending, 5)).text == "ok"


async def test_consumer_cancellation_wakes_waiting_poll(
    client, db_session, app, db_engine, notifications_only
):
    _, body, headers = await enrollment(client, db_session)
    async with _caller(app, db_engine, uuid.UUID(body["node_id"])) as caller:
        pending = asyncio.create_task(caller.get("/health"))
        command = await poll_command(client, headers)
        waiting = asyncio.create_task(
            client.post(
                "/api/runtime/poll",
                headers=headers,
                json={"running": [command["id"]], "slots": 0, "wait": 10},
            )
        )
        await asyncio.sleep(0.3)
        assert not waiting.done()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        result = await asyncio.wait_for(waiting, 5)
        assert result.json()["data"]["cancelled"] == [command["id"]]


async def test_batched_frames_are_idempotent_contiguous_and_single_frames_still_work(
    client, db_session, app, db_engine
):
    _, body, headers = await enrollment(client, db_session)
    async with _caller(app, db_engine, uuid.UUID(body["node_id"])) as caller:
        pending = asyncio.create_task(caller.post("/v1/chat/completions", json={}))
        command = await poll_command(client, headers)
        endpoint = f"/api/runtime/calls/{command['id']}/frames"
        batch = {
            "frames": [
                _frame(0, b"a", status_code=200, content_type="text/event-stream"),
                _frame(1, b"b"),
            ]
        }
        assert (await client.post(endpoint, headers=headers, json=batch)).status_code == 200
        # 丢了响应后原样重发：已落库的序号跳过，不重复写入。
        assert (await client.post(endpoint, headers=headers, json=batch)).status_code == 200
        gap = {"frames": [_frame(3, b"x")]}
        assert (await client.post(endpoint, headers=headers, json=gap)).status_code == 409
        early_end = {"frames": [_frame(2, done=True), _frame(3, b"late")]}
        assert (await client.post(endpoint, headers=headers, json=early_end)).status_code == 409
        too_many = {"frames": [_frame(2 + i) for i in range(65)]}
        assert (await client.post(endpoint, headers=headers, json=too_many)).status_code == 422
        # 旧节点的单帧格式照常可用。
        last = _frame(2, b"c", done=True)
        assert (await client.post(endpoint, headers=headers, json=last)).status_code == 200
        response = await asyncio.wait_for(pending, 5)
        assert response.status_code == 200 and response.text == "abc"
        assert response.headers["content-type"] == "text/event-stream"


async def test_idle_poll_skips_stale_queue_and_heartbeat_settles_it(client, db_session, app):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    call_id = uuid.uuid4()
    payload = json.dumps({"method": "GET", "path": "/health", "body": ""})
    db_session.add(
        RuntimeCall(
            id=call_id,
            node_id=node_id,
            provider="claude",
            request_enc=app.state.cipher.encrypt(payload, envelope_aad(call_id)),
            deadline=now() + timedelta(hours=1),
            consumer_at=now() - timedelta(minutes=5),
        )
    )
    await db_session.commit()
    polled = await client.post("/api/runtime/poll", headers=headers, json={"slots": 4})
    assert polled.json()["data"]["commands"] == []
    await db_session.refresh(await db_session.get(RuntimeCall, call_id))
    assert (await db_session.get(RuntimeCall, call_id)).status == "queued"
    beat = await client.post(
        "/api/runtime/heartbeat",
        headers=headers,
        json={
            "claude": {},
            "codex": {},
            "version": "test",
            "service_status": "foreground",
            "protocol": 2,
        },
    )
    assert beat.status_code == 200 and beat.json()["data"]["protocol"] == 2
    row = await db_session.scalar(
        select(RuntimeCall)
        .where(RuntimeCall.id == call_id)
        .execution_options(populate_existing=True)
    )
    assert row.status == "cancelled" and row.request_enc == ""
    node = await db_session.get(RuntimeNode, node_id)
    await db_session.refresh(node)
    assert node.protocol_version == 2


async def test_notifications_are_routed_by_call_id(app, db_session):
    first, second = uuid.uuid4(), uuid.uuid4()
    wake_first, wake_second = transport.subscribe_call(first), transport.subscribe_call(second)
    try:
        await transport.notify_call(db_session, first, uuid.uuid4())
        await db_session.commit()
        assert await transport.wait_event(wake_first, 5)
        assert not wake_second.is_set()
    finally:
        transport.unsubscribe_call(first, wake_first)
        transport.unsubscribe_call(second, wake_second)


async def test_api_registers_shared_pool_and_fallback_is_a_small_pool(app):
    assert transport.session_factory() is app.state.session_factory
    assert transport.poll_seconds() == transport.FALLBACK_POLL_SECONDS
    transport.configure(None)
    try:
        factory = transport.session_factory()
        engine = factory.kw["bind"]
        assert type(engine.pool).__name__ != "NullPool" and engine.pool.size() == 2
        assert transport.session_factory() is factory
        assert transport.poll_seconds() == transport.UNLISTENED_POLL_SECONDS
        transport._fallback.pop(asyncio.get_running_loop(), None)
        await engine.dispose()
    finally:
        transport.configure(app.state.session_factory, app.state.runtime_listener)


async def test_heartbeat_concurrency_is_listed_and_legacy_nodes_stay_unknown(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    beat = {"claude": {}, "codex": {}, "version": "test", "service_status": "foreground"}

    async def listed():
        nodes = (await client.get("/api/admin/runtime-nodes")).json()["data"]
        row = next(n for n in nodes if n["id"] == body["node_id"])
        return row["max_concurrent"], row["active_calls"], row["protocol_version"]

    endpoint = "/api/runtime/heartbeat"
    assert (await client.post(endpoint, headers=headers, json=beat)).status_code == 200
    assert await listed() == (None, None, 1)
    reported = {**beat, "protocol": 2, "max_concurrent": 6, "active_calls": 2}
    assert (await client.post(endpoint, headers=headers, json=reported)).status_code == 200
    assert await listed() == (6, 2, 2)
    invalid = {**reported, "active_calls": -1}
    assert (await client.post(endpoint, headers=headers, json=invalid)).status_code == 422


async def test_heartbeat_preserves_personal_mode_capability_and_clears_on_omission(
    client, db_session
):
    _, body, headers = await enrollment(client, db_session)
    for declared in ({"feishu_personal_restricted_v1": True}, {}):
        response = await client.post(
            "/api/runtime/heartbeat",
            headers=headers,
            json={"version": "test", "service_status": "launchd", "claude": declared, "codex": {}},
        )
        assert response.status_code == 200
        node = await db_session.get(RuntimeNode, uuid.UUID(body["node_id"]), populate_existing=True)
        assert node.capabilities["claude"].get("feishu_personal_restricted_v1") is bool(declared)


@pytest.mark.parametrize("value", ["true", 1])
async def test_heartbeat_personal_capability_requires_boolean(client, db_session, value):
    _, _, headers = await enrollment(client, db_session)
    response = await client.post(
        "/api/runtime/heartbeat",
        headers=headers,
        json={
            "version": "test",
            "service_status": "launchd",
            "claude": {"feishu_personal_restricted_v1": value},
            "codex": {},
        },
    )
    assert response.status_code == 422
