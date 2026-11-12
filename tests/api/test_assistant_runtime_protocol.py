"""节点协议上的个人助理：沙箱能力的上报、转录的交接、助理调用的领取顺序与并发上限。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest

from coreman.core.assistants import keys, sealed, transcripts
from coreman.core.chat import sessions
from coreman.core.crypto import Cipher
from coreman.core.db.models import RuntimeCall, RuntimeNode
from coreman.core.runtime_nodes.transport import envelope_aad, now
from tests.api.test_runtime_nodes import enrollment
from tests.integration.assistant_helpers import MASTER_KEY, seed_assistant

CAP = {"installed": True, "version": "test", "login": "ready", "models": ["claude-test"]}


@pytest.fixture(autouse=True)
def _master() -> Iterator[None]:
    sealed.use_master(Cipher(MASTER_KEY))
    keys.forget()
    sealed.forget()
    yield
    sealed.use_master(None)
    keys.forget()
    sealed.forget()


async def beat(client, headers, *, identity: dict | None = None, **claude):
    return await client.post(
        "/api/runtime/heartbeat",
        headers=headers,
        json={
            "claude": {**CAP, **claude},
            "codex": CAP,
            "version": "test",
            "service_status": "systemd-user",
            **(identity or {}),
        },
    )


async def test_heartbeat_records_the_sandbox_capability_and_old_nodes_lack_it(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    assert (await beat(client, headers)).status_code == 200
    node = await db_session.get(RuntimeNode, node_id, populate_existing=True)
    assert node is not None and node.capabilities["claude"]["assistant_sandbox_v1"] is False
    assert (await beat(client, headers, assistant_sandbox_v1=True)).status_code == 200
    node = await db_session.get(RuntimeNode, node_id, populate_existing=True)
    assert node is not None and node.capabilities["claude"]["assistant_sandbox_v1"] is True
    assert node.capabilities["codex"]["assistant_sandbox_v1"] is False
    # 只认布尔真值：字符串、数字之类写坏的值当没上报，心跳照常收下，不会被当成「有能力」。
    for garbage in ("yes", 1, "true", None, [True]):
        assert (await beat(client, headers, assistant_sandbox_v1=garbage)).status_code == 200
        node = await db_session.get(RuntimeNode, node_id, populate_existing=True)
        assert node is not None and node.capabilities["claude"]["assistant_sandbox_v1"] is False


async def handover(db_session, node_id: uuid.UUID):
    bot, owner = await seed_assistant(db_session)
    await keys.ensure_key(db_session, sealed.master(), owner.id)
    info = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="oc_1",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    _, plan = await transcripts.prepare(
        db_session,
        bot=bot,
        node_id=node_id,
        info=info,
        session_key="oc_1",
        backend="claude",
        speaker_user_id=None,
    )
    await db_session.commit()
    return bot, plan


async def test_transcript_round_trip_through_the_node_endpoints(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    bot, plan = await handover(db_session, node_id)
    url = f"/api/runtime/assistant-transcripts/{plan.transfer}"
    assert (await client.get(url, headers=headers)).status_code == 404
    put = {**headers, "X-Transcript-Status": "ok", "Content-Type": "application/octet-stream"}
    stored = await client.put(url, headers=put, content=b"tar-bytes")
    assert stored.status_code == 200 and stored.json()["data"]["result"] == "stored"
    assert (await client.put(url, headers=put, content=b"tar-bytes")).status_code == 410
    info = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="oc_1",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    _, again = await transcripts.prepare(
        db_session,
        bot=bot,
        node_id=node_id,
        info=info,
        session_key="oc_1",
        backend="claude",
        speaker_user_id=None,
    )
    await db_session.commit()
    assert again.restore is True
    got = await client.get(f"/api/runtime/assistant-transcripts/{again.transfer}", headers=headers)
    assert got.status_code == 200 and got.content == b"tar-bytes"
    assert got.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("status", "content", "code"),
    [("bogus", b"", 422), ("ok", b"", 422), ("empty", b"x", 422), ("unchanged", b"", 200)],
)
async def test_upload_checks_status_and_body(client, db_session, status, content, code):
    _, body, headers = await enrollment(client, db_session)
    _, plan = await handover(db_session, uuid.UUID(body["node_id"]))
    response = await client.put(
        f"/api/runtime/assistant-transcripts/{plan.transfer}",
        headers={**headers, "X-Transcript-Status": status},
        content=content,
    )
    assert response.status_code == code, response.text


async def test_oversized_upload_is_refused(client, db_session, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_BYTES", 4)
    _, body, headers = await enrollment(client, db_session)
    _, plan = await handover(db_session, uuid.UUID(body["node_id"]))
    response = await client.put(
        f"/api/runtime/assistant-transcripts/{plan.transfer}",
        headers={**headers, "X-Transcript-Status": "ok"},
        content=b"0123456789",
    )
    assert response.status_code == 413


async def test_endpoints_need_the_node_credentials(client, db_session):
    url = f"/api/runtime/assistant-transcripts/{uuid.uuid4()}"
    assert (await client.get(url)).status_code == 401
    assert (await client.put(url, headers={"X-Transcript-Status": "empty"})).status_code == 401


async def queue(
    db_session,
    app,
    node_id: uuid.UUID,
    *,
    sandbox: bool,
    minutes_ago: int,
    provider: str = "claude",
) -> uuid.UUID:
    call_id = uuid.uuid4()
    payload = json.dumps({"method": "POST", "path": "/v1/chat/completions", "body": ""})
    db_session.add(
        RuntimeCall(
            id=call_id,
            node_id=node_id,
            provider=provider,
            request_enc=app.state.cipher.encrypt(payload, envelope_aad(call_id)),
            deadline=now() + timedelta(hours=1),
            consumer_at=now(),
            created_at=now() - timedelta(minutes=minutes_ago),
            sandbox=sandbox,
        )
    )
    await db_session.commit()
    return call_id


async def poll(client, headers, slots: int) -> list[str]:
    result = await client.post("/api/runtime/poll", headers=headers, json={"slots": slots})
    assert result.status_code == 200, result.text
    return [command["id"] for command in result.json()["data"]["commands"]]


async def test_standard_calls_are_claimed_first_and_assistant_calls_are_capped(
    client, db_session, app
):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers, assistant_sandbox_v1=True)
    older = [
        await queue(db_session, app, node_id, sandbox=True, minutes_ago=9 - i) for i in range(3)
    ]
    standard = await queue(db_session, app, node_id, sandbox=False, minutes_ago=1)
    claimed = await poll(client, headers, 4)
    # 普通调用不排在早到的助理调用后面；助理调用按默认上限 2 领取。
    assert claimed == [str(standard), str(older[0]), str(older[1])]
    assert await poll(client, headers, 4) == []
    row = await db_session.get(RuntimeCall, older[0], populate_existing=True)
    assert row is not None
    row.status = "done"
    await db_session.commit()
    assert await poll(client, headers, 4) == [str(older[2])]


async def test_assistant_calls_wait_on_nodes_without_the_capability(client, db_session, app):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers)
    waiting = await queue(db_session, app, node_id, sandbox=True, minutes_ago=5)
    standard = await queue(db_session, app, node_id, sandbox=False, minutes_ago=1)
    assert await poll(client, headers, 4) == [str(standard)]
    row = await db_session.get(RuntimeCall, waiting, populate_existing=True)
    assert row is not None and row.status == "queued"


async def test_concurrency_follows_the_setting(client, db_session, app):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers, assistant_sandbox_v1=True)
    patch = await client.put("/api/admin/settings", json={"assistant_node_concurrency": 1})
    assert patch.status_code == 200, patch.text
    first = await queue(db_session, app, node_id, sandbox=True, minutes_ago=3)
    await queue(db_session, app, node_id, sandbox=True, minutes_ago=2)
    assert await poll(client, headers, 4) == [str(first)]


async def test_heartbeat_records_machine_and_uid_and_drops_garbage(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    fingerprint = "ab" * 32
    ok = await beat(client, headers, identity={"machine": fingerprint, "uid": 1001})
    assert ok.status_code == 200
    node = await db_session.get(RuntimeNode, node_id, populate_existing=True)
    assert node is not None and (node.machine_fingerprint, node.uid) == (fingerprint, 1001)
    # 写坏的值按没上报处理，心跳照常收下；旧节点不带这两项同样是空。
    bad = await beat(client, headers, identity={"machine": "/etc/machine-id", "uid": -1})
    assert bad.status_code == 200
    node = await db_session.get(RuntimeNode, node_id, populate_existing=True)
    assert node is not None and node.machine_fingerprint is None and node.uid is None


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ({"machine": "AB" * 32, "uid": 5}, (None, 5)),
        ({"machine": "ab" * 31, "uid": True}, (None, None)),
        ({"machine": 12345, "uid": "1001"}, (None, None)),
        ({"machine": ["ab" * 32], "uid": 1.5}, (None, None)),
        ({"machine": None, "uid": None}, (None, None)),
        ({"machine": "zz" * 32, "uid": 2**32}, (None, None)),
        ({"machine": "ab" * 32, "uid": 2**32 - 1}, ("ab" * 32, 2**32 - 1)),
        ({"machine": "ab" * 32, "uid": 0}, ("ab" * 32, 0)),
    ],
)
async def test_each_identity_field_is_judged_on_its_own(client, db_session, identity, expected):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    assert (await beat(client, headers, identity=identity)).status_code == 200
    node = await db_session.get(RuntimeNode, node_id, populate_existing=True)
    assert node is not None and (node.machine_fingerprint, node.uid) == expected


async def test_old_daemon_without_new_fields_is_unchanged(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    assert (
        await beat(client, headers, identity={"machine": "ab" * 32, "uid": 7})
    ).status_code == 200
    assert (await beat(client, headers)).status_code == 200
    node = await db_session.get(RuntimeNode, node_id, populate_existing=True)
    assert node is not None and node.machine_fingerprint is None and node.uid is None
    assert set(node.capabilities["claude"]) >= {"installed", "assistant_sandbox_v1"}


async def test_ordinary_only_poll_never_touches_the_assistant_path(
    client, db_session, app, monkeypatch
):
    """只有普通调用的节点：结果与原来一致（按到达顺序、至多 min(slots, 4) 条），不读并发设置、
    不做第二条领取查询。"""
    from sqlalchemy import event

    from coreman.api.routers import runtime_protocol

    async def boom(*args, **kwargs):
        raise AssertionError("assistant path must not run")

    monkeypatch.setattr(runtime_protocol, "_claim_sandboxed", boom)
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers, assistant_sandbox_v1=True)
    calls = [
        await queue(db_session, app, node_id, sandbox=False, minutes_ago=9 - i) for i in range(6)
    ]
    statements: list[str] = []
    engine = app.state.session_factory.kw["bind"].sync_engine

    def record(conn, cursor, statement, *rest):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        claimed = await poll(client, headers, 8)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert claimed == [str(c) for c in calls[:4]]
    claims = [s for s in statements if "FROM runtime_calls" in s and "FOR UPDATE" in s]
    assert len(claims) == 1 and "sandbox IS false" in claims[0]
    assert not any("assistant_node_concurrency" in s for s in statements)


async def test_garbage_concurrency_setting_does_not_break_the_poll(client, db_session, app):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers, assistant_sandbox_v1=True)
    await app.state.settings_store.set("assistant_node_concurrency", "lots")
    standard = await queue(db_session, app, node_id, sandbox=False, minutes_ago=1)
    await queue(db_session, app, node_id, sandbox=True, minutes_ago=5)
    claimed = await poll(client, headers, 4)
    assert claimed[0] == str(standard) and len(claimed) == 2


async def test_unknown_foreign_and_settled_transfers_all_look_the_same(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    _, _other, other_headers = await enrollment(client, db_session)
    _, plan = await handover(db_session, node_id)
    put = {"X-Transcript-Status": "empty"}
    unknown = await client.put(
        f"/api/runtime/assistant-transcripts/{uuid.uuid4()}", headers={**headers, **put}
    )
    foreign = await client.put(
        f"/api/runtime/assistant-transcripts/{plan.transfer}", headers={**other_headers, **put}
    )
    assert unknown.status_code == foreign.status_code == 410
    assert unknown.json() == foreign.json()
    # 真正的节点这时还能用，说明对方的请求没有消耗编号。
    mine = await client.put(
        f"/api/runtime/assistant-transcripts/{plan.transfer}", headers={**headers, **put}
    )
    assert mine.status_code == 200
    settled = await client.put(
        f"/api/runtime/assistant-transcripts/{plan.transfer}", headers={**headers, **put}
    )
    assert settled.status_code == 410 and settled.json() == unknown.json()
    # 下载同样：别人的、没有的、已办完的，一律同一个 404。
    for who, ident in (
        (headers, uuid.uuid4()),
        (other_headers, plan.transfer),
        (headers, plan.transfer),
    ):
        got = await client.get(f"/api/runtime/assistant-transcripts/{ident}", headers=who)
        assert got.status_code == 404
        assert got.json() == {"code": 404, "message": "转录不可用"}


async def test_download_undecryptable_or_destroyed_key_is_a_plain_404(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    bot, plan = await handover(db_session, node_id)
    put = {**headers, "X-Transcript-Status": "ok"}
    assert (
        await client.put(
            f"/api/runtime/assistant-transcripts/{plan.transfer}", headers=put, content=b"abc"
        )
    ).status_code == 200
    info = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="oc_1",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    _, again = await transcripts.prepare(
        db_session,
        bot=bot,
        node_id=node_id,
        info=info,
        session_key="oc_1",
        backend="claude",
        speaker_user_id=None,
    )
    await db_session.commit()
    url = f"/api/runtime/assistant-transcripts/{again.transfer}"
    # 主密钥换了一把：读不出来，只回 404，不报 500。
    sealed.use_master(Cipher(b"\x01" * 32))
    keys.forget()
    sealed.forget()
    got = await client.get(url, headers=headers)
    assert got.status_code == 404 and "traceback" not in got.text.lower()


async def test_upload_storage_failure_is_a_definite_non_retryable_code(
    client, db_session, monkeypatch
):
    from coreman.core.crypto import DecryptError

    _, body, headers = await enrollment(client, db_session)
    _, plan = await handover(db_session, uuid.UUID(body["node_id"]))

    async def broken(*args, **kwargs):
        raise DecryptError("secret detail")

    monkeypatch.setattr(transcripts, "upload", broken)
    response = await client.put(
        f"/api/runtime/assistant-transcripts/{plan.transfer}",
        headers={**headers, "X-Transcript-Status": "ok"},
        content=b"abc",
    )
    assert response.status_code == 410 and "secret" not in response.text


async def test_chunked_oversized_upload_is_cut_off_while_reading(client, db_session, monkeypatch):
    monkeypatch.setattr(transcripts, "MAX_BYTES", 4)
    _, body, headers = await enrollment(client, db_session)
    _, plan = await handover(db_session, uuid.UUID(body["node_id"]))

    async def body_chunks():
        yield b"012"
        yield b"3456789"

    response = await client.put(
        f"/api/runtime/assistant-transcripts/{plan.transfer}",
        headers={**headers, "X-Transcript-Status": "ok"},
        content=body_chunks(),
    )
    assert response.status_code == 413


async def test_assistant_claim_follows_the_calls_own_provider(client, db_session, app):
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers, assistant_sandbox_v1=True)  # 只有 claude 能隔离
    codex = await queue(db_session, app, node_id, sandbox=True, minutes_ago=9, provider="codex")
    claude = await queue(db_session, app, node_id, sandbox=True, minutes_ago=1)
    assert await poll(client, headers, 4) == [str(claude)]
    row = await db_session.get(RuntimeCall, codex, populate_existing=True)
    assert row is not None and row.status == "queued"


async def test_failing_assistant_query_does_not_take_the_ordinary_claim_with_it(
    client, db_session, app, monkeypatch
):
    from sqlalchemy import text

    from coreman.api.routers import runtime_protocol

    async def broken(session, *args, **kwargs):
        await session.execute(text("select 1/0"))  # 数据库里真的报错

    monkeypatch.setattr(runtime_protocol, "_claim_sandboxed", broken)
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers, assistant_sandbox_v1=True)
    standard = await queue(db_session, app, node_id, sandbox=False, minutes_ago=1)
    waiting = await queue(db_session, app, node_id, sandbox=True, minutes_ago=5)
    assert await poll(client, headers, 4) == [str(standard)]
    row = await db_session.get(RuntimeCall, waiting, populate_existing=True)
    assert row is not None and row.status == "queued"
    done = await db_session.get(RuntimeCall, standard, populate_existing=True)
    assert done is not None and done.status == "running"


async def test_settings_read_failing_falls_back_to_two(client, db_session, app, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("pool timeout")

    monkeypatch.setattr(app.state.settings_store, "get", boom)
    _, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    await beat(client, headers, assistant_sandbox_v1=True)
    standard = await queue(db_session, app, node_id, sandbox=False, minutes_ago=1)
    for i in range(3):
        await queue(db_session, app, node_id, sandbox=True, minutes_ago=9 - i)
    assert len(await poll(client, headers, 4)) == 3 and standard


@pytest.mark.parametrize(
    ("stored", "expected"), [(0, 1), (10**9, 32), (-5, 1), ("x", 2), (True, 2)]
)
async def test_stored_concurrency_is_clamped(app, monkeypatch, stored, expected):
    from coreman.api.routers import runtime_protocol

    async def get(*args, **kwargs):
        return stored

    monkeypatch.setattr(app.state.settings_store, "get", get)

    class Req:
        pass

    req = Req()
    req.app = app
    assert await runtime_protocol._assistant_limit(req) == expected


async def test_transfer_slots_are_bounded_and_waiting_times_out(client, db_session, monkeypatch):
    import asyncio

    from coreman.api.routers import runtime_protocol

    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(runtime_protocol, "TRANSFER_SLOTS", slots)
    monkeypatch.setattr(runtime_protocol, "TRANSFER_WAIT_SECONDS", 0.2)
    _, body, headers = await enrollment(client, db_session)
    _, plan = await handover(db_session, uuid.UUID(body["node_id"]))
    url = f"/api/runtime/assistant-transcripts/{plan.transfer}"
    await slots.acquire()
    put = await client.put(url, headers={**headers, "X-Transcript-Status": "empty"})
    got = await client.get(url, headers=headers)
    assert (put.status_code, got.status_code) == (503, 503)
    assert put.json() == {"code": 503, "message": "转录传输繁忙"}
    slots.release()
    ok = await client.put(url, headers={**headers, "X-Transcript-Status": "empty"})
    assert ok.status_code == 200
    assert slots._value == 1  # 用完即还


async def test_slow_sender_is_cut_off_and_frees_its_slot(client, db_session, monkeypatch):
    import asyncio

    from coreman.api.routers import runtime_protocol

    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(runtime_protocol, "TRANSFER_SLOTS", slots)
    monkeypatch.setattr(runtime_protocol, "BODY_SECONDS", 0.2)
    _, body, headers = await enrollment(client, db_session)
    _, plan = await handover(db_session, uuid.UUID(body["node_id"]))

    async def slow():
        yield b"abc"
        await asyncio.sleep(2)
        yield b"def"

    response = await client.put(
        f"/api/runtime/assistant-transcripts/{plan.transfer}",
        headers={**headers, "X-Transcript-Status": "ok"},
        content=slow(),
    )
    assert response.status_code == 408
    assert response.json() == {"code": 408, "message": "转录上传超时"}
    assert slots._value == 1
