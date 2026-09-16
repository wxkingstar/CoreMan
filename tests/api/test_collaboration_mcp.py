import json

from coreman.core.chat.bot_collaboration import issue_capability
from tests.integration.test_collaboration_tools import fresh


async def headers(app, db_session):
    a, b, actor, route, task = await fresh(db_session)
    token = issue_capability(app.state.cipher, task_id=task.id, user_id=str(actor.id))
    return {"Authorization": f"Bearer {token}"}, task, b


async def test_mcp_lifecycle_discovery_and_auth(client, app, db_session):
    auth, task, peer = await headers(app, db_session)
    url = "/api/runtime/collaboration/mcp"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26"},
    }
    assert (await client.post(url, json=body)).status_code == 401
    assert (
        await client.post(url, json=body, headers={**auth, "Origin": "https://evil.example"})
    ).status_code == 403
    response = await client.post(url, json=body, headers=auth)
    assert response.json()["result"]["capabilities"] == {"tools": {}}
    response = await client.post(
        url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=auth
    )
    assert response.status_code == 202
    response = await client.post(
        url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=auth
    )
    assert len(response.json()["result"]["tools"]) == 3
    body = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "search_collaborators", "arguments": {}},
    }
    response = await client.post(url, json=body, headers=auth)
    result = json.loads(response.json()["result"]["content"][0]["text"])
    assert result["items"][0]["id"] == peer.bot_key
    assert (await client.get(url, headers=auth)).status_code == 405


async def test_legacy_endpoint_and_mcp_share_failed_call_budget(client, app, db_session):
    auth, task, peer = await headers(app, db_session)
    for n in range(2):
        response = await client.post(
            "/api/runtime/bot-help",
            headers=auth,
            json={"target_bot_key": f"unknown-{n}", "question": "test"},
        )
        assert response.status_code == 409
    response = await client.post(
        "/api/runtime/collaboration/mcp",
        headers=auth,
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "get_collaborator", "arguments": {"id": "also-unknown"}},
        },
    )
    result = response.json()["result"]
    assert result["isError"] is True
    assert "collaboration_budget_exhausted" in result["content"][0]["text"]
    await db_session.refresh(task)
    assert task.cancel_requested_at is not None


async def test_legacy_long_question_remains_supported(client, app, db_session, monkeypatch):
    from unittest.mock import AsyncMock

    from coreman.core.chat import collaboration_setup

    monkeypatch.setattr(collaboration_setup, "check_current_group", AsyncMock())
    monkeypatch.setattr(collaboration_setup, "available", AsyncMock(return_value=True))
    monkeypatch.setattr(collaboration_setup, "begin_runtime", AsyncMock())
    auth, task, peer = await headers(app, db_session)
    response = await client.post(
        "/api/runtime/bot-help",
        headers=auth,
        json={"target_bot_key": peer.bot_key, "question": "查" * 4000},
    )
    assert response.status_code == 200


async def test_foreign_actor_cannot_discover(client, app, db_session):
    import uuid

    auth, task, peer = await headers(app, db_session)
    token = issue_capability(app.state.cipher, task_id=task.id, user_id=str(uuid.uuid4()))
    response = await client.post(
        "/api/runtime/collaboration/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_collaborators", "arguments": {}},
        },
    )
    assert response.status_code == 403
    assert peer.bot_key not in response.text
