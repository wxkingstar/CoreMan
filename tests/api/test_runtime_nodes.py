import asyncio
import base64
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from coreman.core.db.models import RelayServer, RuntimeCall, RuntimeInstallLink, RuntimeNode
from coreman.core.db.session import make_session_factory
from coreman.core.runtime_nodes.transport import ReverseTransport, now
from tests.api.conftest import login_as


async def test_bot_runtime_label_follows_node_name(client, db_session):
    from coreman.core.db.models import Bot
    from tests.api.test_bots import _bot_body

    _, body, _ = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    relay = await db_session.scalar(
        select(RelayServer).where(
            RelayServer.runtime_node_id == node_id, RelayServer.model_provider == "claude"
        )
    )
    created = await client.post("/api/admin/bots", json=_bot_body())
    assert created.status_code == 201, created.text
    bot_id = created.json()["data"]["id"]
    bot = await db_session.get(Bot, uuid.UUID(bot_id))
    bot.relay_server_id = relay.id
    await db_session.commit()

    async def check_label(expected):
        detail = (await client.get(f"/api/admin/bots/{bot_id}")).json()["data"]
        rows = (await client.get("/api/admin/bots")).json()["data"]["items"]
        assert detail["relay_name"] == expected
        assert next(row for row in rows if row["id"] == bot_id)["relay_name"] == expected
        assert detail["relay_server_id"] == str(relay.id)

    await check_label("Test runtime / claude")
    renamed = await client.patch(f"/api/admin/runtime-nodes/{node_id}", json={"name": "研发运行时"})
    assert renamed.status_code == 200, renamed.text
    await check_label("研发运行时 / claude")


async def test_claimed_request_gets_read_budget_for_headers(client, db_session, app, db_engine):
    _, body, headers = await enrollment(client, db_session)
    transport = ReverseTransport(
        uuid.UUID(body["node_id"]),
        "codex",
        factory=make_session_factory(db_engine),
        cipher=app.state.cipher,
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://node/codex", timeout=httpx.Timeout(2, connect=0.3)
    ) as caller:
        pending = asyncio.create_task(caller.get("/health"))
        command = await poll_command(client, headers)
        await asyncio.sleep(0.5)  # Exceeds connect budget but not accepted/read budget.
        assert not pending.done()
        response = await client.post(
            f"/api/runtime/calls/{command['id']}/frames",
            headers=headers,
            json={
                "seq": 0,
                "status_code": 200,
                "done": True,
                "data": base64.b64encode(b"ok").decode(),
            },
        )
        assert response.status_code == 200
        assert (await pending).text == "ok"


async def test_abandoned_execution_is_failed_without_replay(client, db_session, app, db_engine):
    _, body, headers = await enrollment(client, db_session)
    transport = ReverseTransport(
        uuid.UUID(body["node_id"]),
        "claude",
        factory=make_session_factory(db_engine),
        cipher=app.state.cipher,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://node", timeout=3) as caller:
        pending = asyncio.create_task(caller.get("/health"))
        command = await poll_command(client, headers)
        response = await client.post(
            "/api/runtime/poll", headers=headers, json={"abandoned": [command["id"]], "slots": 4}
        )
        assert response.json()["data"]["commands"] == []
        with pytest.raises(httpx.ConnectError, match="control_lost"):
            await pending


async def enrollment(client, db_session):
    await login_as(client, db_session, role="platform_admin")
    response = await client.post(
        "/api/admin/runtime-nodes/install-links",
        json={
            "workspace_root": "/home/ai/projects",
            "name": "Test runtime",
        },
    )
    assert response.status_code == 200, response.text
    link = response.json()["data"]
    token = link["url"].split("/install/")[1].split("/")[0]
    body = {
        "install_token": token,
        "node_id": str(uuid.uuid4()),
        "node_token": "n" * 43,
        "hostname": "testhost",
        "username": "ai",
        "platform": "linux",
        "architecture": "amd64",
        "version": "test",
        "workspace_root": "/home/ai/projects",
    }
    response = await client.post("/api/runtime/enroll", json=body)
    assert response.status_code == 200, response.text
    headers = {"X-Runtime-ID": body["node_id"], "Authorization": "Bearer " + body["node_token"]}
    return link, body, headers


async def test_root_update_requires_daemon_ack(client, db_session):
    _, body, headers = await enrollment(client, db_session)
    url = f"/api/admin/runtime-nodes/{body['node_id']}"
    beat = {
        "claude": {"installed": True, "login": "ready"},
        "codex": {},
        "version": "test",
        "service_status": "foreground",
        "root_edit_supported": True,
    }
    await client.post("/api/runtime/heartbeat", headers=headers, json=beat)
    response = await client.patch(url, json={"workspace_root": "/home/ai/new"})
    assert response.status_code == 200, response.text
    node = (await client.get("/api/admin/runtime-nodes")).json()["data"][0]
    assert node["workspace_root"] == "/home/ai/projects"
    change = node["root_change"]
    assert change["status"] == "pending"
    assert (await client.patch(url, json={"workspace_root": "/home/ai/other"})).status_code == 409
    result = await client.post("/api/runtime/heartbeat", headers=headers, json=beat)
    assert result.json()["data"]["root_change"] == change
    await client.post(
        "/api/runtime/heartbeat",
        headers=headers,
        json={**beat, "root_change_result": {**change, "status": "applied"}},
    )
    node = (await client.get("/api/admin/runtime-nodes")).json()["data"][0]
    assert node["workspace_root"] == "/home/ai/new"
    assert node["root_change"]["status"] == "applied"


@pytest.mark.parametrize(
    "cap,reason",
    [
        ({}, "not_installed"),
        ({"installed": False}, "not_installed"),
        ({"installed": True, "login": "required"}, "login_required"),
        ({"installed": True, "login": "unknown"}, "unknown"),
        ({"installed": True, "login": "ready"}, None),
    ],
)
async def test_backend_readiness_controls_creation(client, db_session, monkeypatch, cap, reason):
    from unittest.mock import AsyncMock

    from coreman.core.bots import workspace_transfer
    from tests.api.test_bots import _bot_body

    monkeypatch.setattr(
        workspace_transfer,
        "call_agent",
        AsyncMock(return_value={"success": True, "workspace_protocol": 1}),
    )
    _, body, headers = await enrollment(client, db_session)
    await client.post(
        "/api/runtime/heartbeat",
        headers=headers,
        json={"claude": cap, "codex": {}, "version": "test", "service_status": "foreground"},
    )
    relay = (await client.get("/api/admin/runtime-nodes")).json()["data"][0]["backends"]
    claude = next(r for r in relay if r["model_provider"] == "claude")
    assert claude["unavailable_reason"] == reason
    response = await client.post(
        "/api/admin/bots",
        json={
            **_bot_body(),
            "relay_server_id": claude["id"],
            "working_dir": "/home/ai/projects/test",
        },
    )
    assert response.status_code == (201 if reason is None else 422), response.text


async def test_root_update_guards_and_failed_ack(client, db_session):
    from coreman.core.bots.workspace import reserve_workspace
    from coreman.core.errors import ApiError

    _, body, headers = await enrollment(client, db_session)
    url = f"/api/admin/runtime-nodes/{body['node_id']}"
    # Older runtimes must never report a root as saved without applying it.
    assert (await client.patch(url, json={"workspace_root": "/home/new"})).status_code == 409
    for path in ("/", "relative", "/home/../etc", "//home/ai"):
        assert (await client.patch(url, json={"workspace_root": path})).status_code == 422
    beat = {
        "claude": {"installed": True, "login": "ready"},
        "codex": {},
        "version": "test",
        "service_status": "foreground",
        "root_edit_supported": True,
    }
    await client.post("/api/runtime/heartbeat", headers=headers, json=beat)
    await client.patch(url, json={"workspace_root": "/home/new"})
    node = (await client.get("/api/admin/runtime-nodes")).json()["data"][0]
    change = node["root_change"]
    with pytest.raises(ApiError, match="等待"):
        await reserve_workspace(
            db_session, uuid.UUID(node["backends"][0]["id"]), "/home/ai/projects/new"
        )
    await db_session.rollback()
    # Response from an old request cannot complete the current change.
    await client.post(
        "/api/runtime/heartbeat",
        headers=headers,
        json={**beat, "root_change_result": {**change, "id": "stale", "status": "applied"}},
    )
    node = (await client.get("/api/admin/runtime-nodes")).json()["data"][0]
    assert node["root_change"]["status"] == "pending"
    await client.post(
        "/api/runtime/heartbeat",
        headers=headers,
        json={**beat, "root_change_result": {**change, "status": "failed"}},
    )
    node = (await client.get("/api/admin/runtime-nodes")).json()["data"][0]
    assert node["workspace_root"] == "/home/ai/projects"
    assert node["root_change"]["status"] == "failed"
    await reserve_workspace(
        db_session, uuid.UUID(node["backends"][0]["id"]), "/home/ai/projects/new"
    )
    await db_session.rollback()


async def test_root_change_serializes_with_workspace_allocation(client, db_session):
    from coreman.core.bots.workspace import reserve_workspace
    from coreman.core.db.models import Bot
    from tests.api.test_bots import _bot_body

    _, body, headers = await enrollment(client, db_session)
    beat = {
        "claude": {},
        "codex": {},
        "version": "test",
        "service_status": "foreground",
        "root_edit_supported": True,
    }
    await client.post("/api/runtime/heartbeat", headers=headers, json=beat)
    created = (await client.post("/api/admin/bots", json=_bot_body())).json()["data"]
    relay = await db_session.scalar(
        select(RelayServer).where(RelayServer.runtime_node_id == uuid.UUID(body["node_id"]))
    )
    bot = await db_session.get(Bot, uuid.UUID(created["id"]))
    await reserve_workspace(db_session, relay.id, "/home/ai/projects/new")
    editing = asyncio.create_task(
        client.patch(
            f"/api/admin/runtime-nodes/{body['node_id']}", json={"workspace_root": "/home/new"}
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert not editing.done()
        bot.relay_server_id = relay.id
        bot.working_dir = "/home/ai/projects/new"
        await db_session.commit()
        response = await asyncio.wait_for(editing, 3)
        assert response.status_code == 409, response.text
    finally:
        await db_session.rollback()
        if not editing.done():
            editing.cancel()
            await asyncio.gather(editing, return_exceptions=True)


@pytest.mark.parametrize("incoming", [False, True])
async def test_root_update_rejects_bound_or_incoming_employees(client, db_session, incoming):
    from coreman.core.db.models import Bot
    from tests.api.test_bots import _bot_body

    _, body, headers = await enrollment(client, db_session)
    beat = {
        "claude": {},
        "codex": {},
        "version": "test",
        "service_status": "foreground",
        "root_edit_supported": True,
    }
    await client.post("/api/runtime/heartbeat", headers=headers, json=beat)
    bot_data = (await client.post("/api/admin/bots", json=_bot_body())).json()["data"]
    bot = await db_session.get(Bot, uuid.UUID(bot_data["id"]))
    relay = await db_session.scalar(
        select(RelayServer).where(RelayServer.runtime_node_id == uuid.UUID(body["node_id"]))
    )
    if incoming:
        bot.workspace_target_relay_id = relay.id
    else:
        bot.relay_server_id = relay.id
    await db_session.commit()
    response = await client.patch(
        f"/api/admin/runtime-nodes/{body['node_id']}", json={"workspace_root": "/home/new"}
    )
    assert response.status_code == 409
    assert "AI 员工" in response.text


async def poll_command(client, headers):
    for _ in range(100):
        result = await client.post("/api/runtime/poll", json={"slots": 1}, headers=headers)
        assert result.status_code == 200, result.text
        commands = result.json()["data"]["commands"]
        if commands:
            return commands[0]
        await asyncio.sleep(0.03)
    pytest.fail("no outbound command received")


async def test_link_enrollment_retry_capabilities_and_disable(client, db_session):
    link, body, headers = await enrollment(client, db_session)
    assert (await client.post("/api/runtime/enroll", json=body)).status_code == 200
    altered = {**body, "node_id": str(uuid.uuid4())}
    assert (await client.post("/api/runtime/enroll", json=altered)).status_code == 409
    cap = {"installed": True, "version": "test", "login": "ready", "models": ["claude-test"]}
    response = await client.post(
        "/api/runtime/heartbeat",
        headers=headers,
        json={
            "claude": cap,
            "codex": {**cap, "models": ["codex/test"]},
            "version": "test",
            "service_status": "systemd-user",
        },
    )
    assert response.status_code == 200, response.text
    nodes = (await client.get("/api/admin/runtime-nodes")).json()["data"]
    assert len(nodes) == 1
    assert nodes[0]["online"] is True
    assert len(nodes[0]["backends"]) == 2
    assert nodes[0]["backends"][0]["workspace_root"] == "/home/ai/projects"
    bad_headers = {**headers, "X-Runtime-ID": str(uuid.uuid4())}
    assert (await client.post("/api/runtime/poll", headers=bad_headers, json={})).status_code == 401
    assert (
        await client.patch(f"/api/admin/runtime-nodes/{body['node_id']}", json={"is_active": False})
    ).status_code == 200
    assert (await client.post("/api/runtime/poll", headers=headers, json={})).status_code == 401
    assert (await client.get(link["url"].replace("http://testserver", ""))).status_code == 410


async def test_codex_catalog_changes_survive_legacy_heartbeats(client, db_session):
    _, _, headers = await enrollment(client, db_session)
    heartbeat = {
        "claude": {"installed": True, "models": ["claude-test"]},
        "codex": {"installed": True, "models": ["codex/gpt-6-astra"]},
        "version": "test",
        "service_status": "systemd-user",
    }

    async def beat():
        response = await client.post("/api/runtime/heartbeat", headers=headers, json=heartbeat)
        assert response.status_code == 200, response.text

    async def backends():
        response = await client.get("/api/admin/runtime-nodes")
        assert response.status_code == 200, response.text
        return {b["model_provider"]: b for b in response.json()["data"][0]["backends"]}

    await beat()
    response = await client.post(
        "/api/admin/model-catalog",
        json={"provider": "codex", "model": "codex/gpt-6", "sort_order": 95},
    )
    assert response.status_code == 201, response.text
    assert "codex/gpt-6" in (await backends())["codex"]["effective_models"]
    await beat()  # An old daemon must not remove the newly configured model.
    rows = await backends()
    assert rows["codex"]["supported_models_mode"] == "inherit"
    assert "codex/gpt-6" in rows["codex"]["effective_models"]
    assert "claude-test" in rows["claude"]["effective_models"]
    assert "codex/gpt-6" not in rows["claude"]["effective_models"]

    response = await client.patch(
        "/api/admin/model-catalog/codex/codex/gpt-6", json={"retired": True}
    )
    assert response.status_code == 200, response.text
    assert "codex/gpt-6" not in (await backends())["codex"]["effective_models"]

    heartbeat["codex"]["installed"] = False
    await beat()
    assert (await backends())["codex"]["effective_models"] == []
    heartbeat["codex"]["installed"] = True
    await beat()
    assert "codex/gpt-6-astra" in (await backends())["codex"]["effective_models"]


async def test_claude_catalog_additions_and_retirements_override_old_discovery(client, db_session):
    _, _, headers = await enrollment(client, db_session)
    for model in ["claude-future", "minimax/new"]:
        response = await client.post(
            "/api/admin/model-catalog", json={"provider": "claude", "model": model}
        )
        assert response.status_code == 201, response.text
    response = await client.patch(
        "/api/admin/model-catalog/claude/claude-opus-5", json={"retired": True}
    )
    assert response.status_code == 200, response.text
    for installed in [True, False, True]:
        response = await client.post(
            "/api/runtime/heartbeat",
            headers=headers,
            json={
                "version": "test",
                "service_status": "foreground",
                "codex": {},
                "claude": {"installed": installed, "models": ["claude-opus-5"]},
            },
        )
        assert response.status_code == 200, response.text
        nodes = (await client.get("/api/admin/runtime-nodes")).json()["data"]
        models = next(b for b in nodes[0]["backends"] if b["model_provider"] == "claude")[
            "effective_models"
        ]
        assert ("claude-future" in models) == installed
        assert "claude-opus-5" not in models
        assert "minimax/new" not in models


async def test_reverse_stream_cross_session_and_cancel(client, db_session, app, db_engine):
    _, body, headers = await enrollment(client, db_session)
    transport = ReverseTransport(
        uuid.UUID(body["node_id"]),
        "claude",
        factory=make_session_factory(db_engine),
        cipher=app.state.cipher,
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://node/claude", timeout=5
    ) as caller:
        ready = asyncio.Event()
        release = asyncio.Event()
        output = []

        async def consume():
            async with caller.stream(
                "POST", "/v1/chat/completions", json={"env_vars": {"secret": "private"}}
            ) as response:
                assert response.status_code == 200
                async for part in response.aiter_bytes():
                    output.append(part)
                    ready.set()
                    await release.wait()
                    break

        consumer = asyncio.create_task(consume())
        command = await poll_command(client, headers)
        assert command["path"] == "/v1/chat/completions"
        assert b'"private"' in base64.b64decode(command["body"])
        call_id = uuid.UUID(command["id"])
        async with make_session_factory(db_engine)() as session:
            row = await session.get(RuntimeCall, call_id)
            assert row.request_enc == ""  # handed-off secrets aren't retained
        payload = {
            "seq": 0,
            "status_code": 200,
            "content_type": "text/event-stream",
            "data": base64.b64encode(b"data: hello\n\n").decode(),
        }
        endpoint = f"/api/runtime/calls/{call_id}/frames"
        assert (await client.post(endpoint, json=payload, headers=headers)).status_code == 200
        # A lost response may be retried without duplicating the stream.
        assert (await client.post(endpoint, json=payload, headers=headers)).status_code == 200
        await asyncio.wait_for(ready.wait(), 5)
        release.set()
        await consumer
        assert output == [b"data: hello\n\n"]
        result = await client.post(
            "/api/runtime/poll", json={"running": [str(call_id)]}, headers=headers
        )
        assert result.json()["data"]["cancelled"] == [str(call_id)]
        assert (
            await client.post(endpoint, json={"seq": 1, "done": True}, headers=headers)
        ).status_code == 410


async def test_install_revocation_expiry_permissions_and_script_quoting(client, db_session):
    await login_as(client, db_session, role="member")
    assert (
        await client.post("/api/admin/runtime-nodes/install-links", json={"workspace_root": "/x"})
    ).status_code == 403
    await login_as(client, db_session, role="platform_admin")
    path = "/data/projects space;literal$(echo)"
    response = await client.post(
        "/api/admin/runtime-nodes/install-links", json={"workspace_root": path}
    )
    link = response.json()["data"]
    route = link["url"].replace("http://testserver", "")
    script = await client.get(route)
    assert script.status_code == 200
    assert path not in script.text  # config is base64, never executable shell interpolation
    assert "no-store" in script.headers["cache-control"]
    row = await db_session.get(RuntimeInstallLink, uuid.UUID(link["id"]))
    assert row.options["max_concurrent"] == 10
    assert timedelta(hours=23, minutes=59) < row.expires_at - now() <= timedelta(hours=24)
    await db_session.execute(
        update(RuntimeInstallLink)
        .where(RuntimeInstallLink.id == uuid.UUID(link["id"]))
        .values(expires_at=now() - timedelta(seconds=1))
    )
    await db_session.commit()
    assert (await client.get(route)).status_code == 410
    await client.delete(f"/api/admin/runtime-nodes/install-links/{link['id']}")
    assert (await client.get(route)).status_code == 401
    assert (
        await client.post("/api/admin/runtime-nodes/install-links", json={"workspace_root": "/"})
    ).status_code == 422


async def test_no_duplicate_claim_and_node_isolation(client, db_session, app, db_engine):
    _, body, headers = await enrollment(client, db_session)
    transport = ReverseTransport(
        uuid.UUID(body["node_id"]),
        "codex",
        factory=make_session_factory(db_engine),
        cipher=app.state.cipher,
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://node/codex", timeout=5
    ) as caller:
        task = asyncio.create_task(caller.get("/v1/models"))
        command = await poll_command(client, headers)
        second = await client.post("/api/runtime/poll", json={}, headers=headers)
        assert second.json()["data"]["commands"] == []
        frame = {
            "seq": 0,
            "status_code": 200,
            "data": base64.b64encode(b'{"data":[]}').decode(),
            "done": True,
        }
        await client.post(f"/api/runtime/calls/{command['id']}/frames", json=frame, headers=headers)
        assert (await task).json() == {"data": []}
    rows = list(
        await db_session.scalars(
            select(RelayServer).where(RelayServer.runtime_node_id == uuid.UUID(body["node_id"]))
        )
    )
    assert len(rows) == 2
    assert all(r.agent_token_enc for r in rows)
    node = await db_session.get(RuntimeNode, uuid.UUID(body["node_id"]))
    assert node.token_hash != body["node_token"]


async def test_reverse_timeout_cancels_unclaimed_request(client, db_session, app, db_engine):
    _, body, _ = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    transport = ReverseTransport(
        node_id, "claude", factory=make_session_factory(db_engine), cipher=app.state.cipher
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://node", timeout=0.1
    ) as caller:
        with pytest.raises(httpx.ConnectTimeout):
            await caller.get("/health")
    async with make_session_factory(db_engine)() as session:
        row = await session.scalar(select(RuntimeCall).where(RuntimeCall.node_id == node_id))
        assert row.status == "cancelled"
        assert row.request_enc == ""


async def test_authenticated_session_view_has_no_external_dependencies(client, db_session):
    _, body, _ = await enrollment(client, db_session)
    from coreman.core.db.models import ChatLog
    from tests.api.test_chat_logs import _seed

    await _seed(db_session)
    row = (await db_session.scalars(select(ChatLog))).first()
    row.relay_session_id = uuid.uuid4()
    await db_session.commit()
    url = f"/api/admin/runtime-nodes/{body['node_id']}/claude/session/{row.relay_session_id}"
    response = await client.get(url)
    assert response.status_code == 200
    assert "new EventSource(wsUrl)" in response.text
    assert "location.pathname + '/events' + location.search" in response.text
    assert "const marked = {setOptions() {}}" in response.text
    assert '<script src="https://' not in response.text
    assert "return marked.parse(text)" not in response.text
    await login_as(client, db_session, role="member")
    assert (await client.get(url)).status_code == 403


def _link(app, node_id, session_id, user_id, bot_id, **kw):
    from coreman.core import session_links

    return session_links.issue(
        app.state.cipher,
        session_id=uuid.UUID(str(session_id)),
        user_id=user_id,
        node_id=uuid.UUID(str(node_id)),
        provider="claude",
        bot_id=bot_id,
        **kw,
    )


async def _private_session(client, db_session, platform="feishu", upgraded=True):
    from coreman.core.db.models import ChatLog
    from tests.api.test_chat_logs import _seed

    _, body, _ = await enrollment(client, db_session)
    node = await db_session.get(RuntimeNode, uuid.UUID(body["node_id"]))
    node.capabilities = {"claude": {"owner_session_view_v1": upgraded}}
    mine, _, owner = await _seed(db_session)
    row = (await db_session.scalars(select(ChatLog).where(ChatLog.user_id == owner.id))).first()
    row.platform, row.chat_type = platform, "single"
    row.relay_session_id = uuid.uuid4()
    await db_session.commit()
    url = f"/api/admin/runtime-nodes/{body['node_id']}/claude/session/{row.relay_session_id}"
    return body["node_id"], mine, owner, row, url


def _mock_events(monkeypatch):
    monkeypatch.setattr(
        "coreman.core.runtime_nodes.transport.ReverseTransport",
        lambda node_id, provider: httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"data: {}\n\n")
        ),
    )


async def test_private_runtime_viewer_blocks_admin_and_bot_token(
    app, client, db_session, monkeypatch
):
    from coreman.api import bot_auth
    from tests.api.conftest import login_existing

    node_id, _, owner, _, url = await _private_session(client, db_session)
    # 飞书私聊可能含个人飞书资料：别的管理员看不了。
    for suffix in ("", "/events"):
        assert (await client.get(url + suffix)).status_code == 404
    owner.role = "platform_admin"
    await db_session.commit()
    await login_existing(client, db_session, owner)
    assert (await client.get(url)).status_code == 200

    async def token_user(request, session):
        return owner

    monkeypatch.setattr(bot_auth, "token_user", token_user)
    client.cookies.set("bot_token", "verified-owner-token")
    for suffix in ("", "/events"):
        assert (await client.get(url + suffix)).status_code == 404
    unknown = f"/api/admin/runtime-nodes/{node_id}/claude/session/{uuid.uuid4()}"
    assert (await client.get(unknown)).status_code == 404


@pytest.mark.parametrize("platform", ["feishu", "wecom"])
async def test_member_owner_views_own_private_chat_and_is_audited(
    app, client, db_session, monkeypatch, platform
):
    from coreman.core.db.models import AuditLog
    from tests.api.conftest import login_existing

    node_id, mine, owner, row, url = await _private_session(client, db_session, platform)
    _mock_events(monkeypatch)
    await login_existing(client, db_session, owner)
    assert owner.role == "member"
    # 普通私聊登录即可看自己的会话；过期的链接不妨碍本人。
    expired = _link(app, node_id, row.relay_session_id, owner.id, mine.id, now=0)
    page = await client.get(f"{url}?t={expired}")
    assert page.status_code == 200
    assert "location.pathname + '/events' + location.search" in page.text
    assert page.headers["referrer-policy"] == "no-referrer"
    assert (await client.get(f"{url}/events")).status_code == 200
    link = _link(app, node_id, row.relay_session_id, owner.id, mine.id)
    assert (await client.get(f"{url}?t={link}")).status_code == 200
    audits = (
        await db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "runtime.session_view")
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    ).all()
    assert [a.diff["via"][1] for a in audits] == ["owner", "link"]
    assert {(a.actor_id, a.target_id) for a in audits} == {(owner.id, str(row.relay_session_id))}


async def test_private_chat_stays_closed_to_other_people_and_mixed_history(app, client, db_session):
    from coreman.core.db.models import ChatLog
    from tests.api.conftest import login_as, login_existing

    node_id, mine, owner, row, url = await _private_session(client, db_session)
    stranger = await login_as(client, db_session)
    good = _link(app, node_id, row.relay_session_id, owner.id, mine.id)
    # 链接转给别人：对方登录后也打不开，并提示链接不属于他。
    denied = await client.get(f"{url}?t={good}")
    assert denied.status_code == 403 and "链接已失效或不属于当前登录账号" in denied.text
    assert (await client.get(url)).status_code == 403
    await login_existing(client, db_session, owner)
    assert (await client.get(url)).status_code == 200
    db_session.add(
        ChatLog(
            **{
                c: getattr(row, c)
                for c in ("bot_id", "bot_key", "platform", "chat_id", "message_type", "status")
            },
            chat_type="single",
            user_id=stranger.id,
            relay_session_id=row.relay_session_id,
            request_at=row.request_at,
        )
    )
    await db_session.commit()
    for query in ("", f"?t={good}"):
        assert (await client.get(f"{url}{query}")).status_code == 403


async def test_first_turn_needs_a_valid_link(app, client, db_session):
    import time

    from coreman.core.session_links import TTL_SECONDS
    from tests.api.conftest import login_existing

    node_id, mine, owner, _, _ = await _private_session(client, db_session)
    fresh = uuid.uuid4()
    url = f"/api/admin/runtime-nodes/{node_id}/claude/session/{fresh}"
    # 还没写日志的会话没有归属证据：管理员看不了，本人也只能凭链接。
    assert (await client.get(url)).status_code == 404
    await login_existing(client, db_session, owner)
    assert (await client.get(url)).status_code == 403
    for bad in (
        _link(app, node_id, fresh, uuid.uuid4(), mine.id),
        _link(app, uuid.uuid4(), fresh, owner.id, mine.id),
        _link(app, node_id, uuid.uuid4(), owner.id, mine.id),
        _link(app, node_id, fresh, owner.id, mine.id, now=time.time() - TTL_SECONDS),
        "not-a-token",
    ):
        for suffix in ("", "/events"):
            assert (await client.get(f"{url}{suffix}?t={bad}")).status_code == 403
    good = _link(app, node_id, fresh, owner.id, mine.id)
    assert (await client.get(f"{url}?t={good[:-4]}AAAA")).status_code == 403
    assert (await client.get(f"{url}?t={good}")).status_code == 200


async def test_old_runtime_keeps_private_sessions_for_admins_only(app, client, db_session):
    from tests.api.conftest import login_existing

    node_id, mine, owner, row, url = await _private_session(
        client, db_session, "wecom", upgraded=False
    )
    link = _link(app, node_id, row.relay_session_id, owner.id, mine.id)
    await login_existing(client, db_session, owner)
    for query in ("", f"?t={link}"):
        denied = await client.get(url + query)
        assert denied.status_code == 403 and "升级运行时" in denied.text
    assert (await client.get(f"{url}/events?t={link}")).status_code == 403
    owner.role = "ai_committee"
    await db_session.commit()
    assert (await client.get(f"{url}?t={link}")).status_code == 200


@pytest.mark.parametrize("role", ["member", "platform_admin"])
async def test_personal_session_needs_link_bound_to_context_epoch(app, client, db_session, role):
    from coreman.core.db.models import FeishuPersonalGrant, Task
    from tests.api.conftest import login_existing

    node_id, mine, owner, row, url = await _private_session(client, db_session)
    task = Task(
        bot_id=mine.id,
        kind="chat",
        payload={},
        status="failed",
        result={"feishu_personal": True},
    )
    grant = FeishuPersonalGrant(
        bot_id=mine.id,
        user_id=owner.id,
        app_id="cli_test",
        platform_user_id="human",
        open_id="ou_human",
        tenant_key="tenant-test",
        status="connected",
    )
    db_session.add_all([task, grant])
    await db_session.flush()
    row.task_id = task.id
    owner.role = role
    await db_session.commit()
    await login_existing(client, db_session, owner)
    # 飞书资料模式：本人（哪怕是管理员）也只能凭 24 小时内的链接看。
    denied = await client.get(url)
    assert denied.status_code == 403 and "飞书资料模式" in denied.text
    link = _link(
        app, node_id, row.relay_session_id, owner.id, mine.id, context_epoch=grant.context_epoch
    )
    assert (await client.get(f"{url}?t={link}")).status_code == 200
    plain = _link(app, node_id, row.relay_session_id, owner.id, mine.id)
    assert (await client.get(f"{url}?t={plain}")).status_code == 200
    # 撤销、切换模式、重新授权都会换上下文版本。
    grant.context_epoch = uuid.uuid4()
    await db_session.commit()
    assert (await client.get(f"{url}?t={link}")).status_code == 403
    await db_session.delete(grant)
    await db_session.commit()
    assert (await client.get(f"{url}?t={link}")).status_code == 403


async def test_session_link_sends_signed_out_viewer_to_login(app, client, db_session):
    from urllib.parse import quote

    node_id, mine, owner, row, url = await _private_session(client, db_session)
    link = _link(app, node_id, row.relay_session_id, owner.id, mine.id)
    client.cookies.clear()
    response = await client.get(f"{url}?t={link}")
    assert response.status_code == 302
    assert response.headers["location"] == "/login?redirect=" + quote(f"{url}?t={link}", safe="")
    assert (await client.get(f"{url}/events?t={link}")).status_code == 401


async def test_team_change_moves_node_and_its_instances(client, db_session):
    from coreman.core.db.models import Team

    _, body, _ = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    team = Team(slug=f"t{uuid.uuid4().hex[:6]}", name_zh="研发")
    db_session.add(team)
    await db_session.commit()
    team_id = team.id
    url = f"/api/admin/runtime-nodes/{node_id}"

    async def teams():
        db_session.expire_all()
        node = await db_session.get(RuntimeNode, node_id)
        relays = await db_session.scalars(
            select(RelayServer.team_id).where(RelayServer.runtime_node_id == node_id)
        )
        return node.team_id, set(relays)

    assert (await client.patch(url, json={"team_id": str(team_id)})).status_code == 200
    assert await teams() == (team_id, {team_id})
    row = (await client.get("/api/admin/runtime-nodes")).json()["data"][0]
    assert (row["team_id"], row["team_name"]) == (str(team_id), "研发")
    # 只改名称不会动团队；显式传 null 改回公共池。
    assert (await client.patch(url, json={"name": "研发机"})).status_code == 200
    assert await teams() == (team_id, {team_id})
    assert (await client.patch(url, json={"team_id": None})).status_code == 200
    assert await teams() == (None, {None})
    missing = await client.patch(url, json={"team_id": str(uuid.uuid4())})
    assert missing.status_code == 422
    assert await teams() == (None, {None})


async def test_delete_runtime_requires_unbound_bots_and_revokes_node(client, db_session):
    from coreman.core.db.models import AuditLog, Bot
    from tests.api.test_bots import _bot_body

    link, body, headers = await enrollment(client, db_session)
    node_id = uuid.UUID(body["node_id"])
    relay_ids = list(
        await db_session.scalars(
            select(RelayServer.id).where(RelayServer.runtime_node_id == node_id)
        )
    )
    created = await client.post("/api/admin/bots", json=_bot_body())
    assert created.status_code == 201, created.text
    bot = await db_session.get(Bot, uuid.UUID(created.json()["data"]["id"]))
    bot.relay_server_id = relay_ids[0]
    await db_session.commit()
    url = f"/api/admin/runtime-nodes/{node_id}"

    blocked = await client.delete(url)
    assert blocked.status_code == 409
    assert "销售助手" in blocked.json()["message"]
    # 正在迁移到该节点的 AI 员工同样占用它。
    bot.relay_server_id, bot.workspace_target_relay_id = None, relay_ids[1]
    await db_session.commit()
    assert (await client.delete(url)).status_code == 409
    bot.workspace_target_relay_id = None
    await db_session.commit()

    call = RuntimeCall(
        node_id=node_id,
        provider="claude",
        request_enc="x",
        deadline=now() + timedelta(minutes=5),
        consumer_at=now(),
    )
    db_session.add(call)
    await db_session.commit()
    call_id = call.id
    deleted = await client.delete(url)
    assert deleted.status_code == 200, deleted.text
    db_session.expire_all()
    assert await db_session.get(RuntimeNode, node_id) is None
    assert await db_session.get(RuntimeCall, call_id) is None
    assert not list(
        await db_session.scalars(select(RelayServer).where(RelayServer.id.in_(relay_ids)))
    )
    stored = await db_session.get(RuntimeInstallLink, uuid.UUID(link["id"]))
    assert stored.node_id is None and stored.used_at is not None
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "runtime.delete"))
    assert audit.target_id == str(node_id)
    assert audit.diff["name"] == ["Test runtime", None]
    assert (await client.get("/api/admin/runtime-nodes")).json()["data"] == []
    # 节点凭证失效，也不能凭旧安装链接重新注册出同一节点。
    assert (await client.post("/api/runtime/poll", headers=headers, json={})).status_code == 401
    assert (await client.post("/api/runtime/enroll", json=body)).status_code == 409
    assert (await client.delete(url)).status_code == 404


async def test_members_cannot_edit_or_delete_runtime(client, db_session):
    _, body, _ = await enrollment(client, db_session)
    await login_as(client, db_session, role="member")
    url = f"/api/admin/runtime-nodes/{body['node_id']}"
    assert (await client.patch(url, json={"team_id": None})).status_code == 403
    assert (await client.delete(url)).status_code == 403
