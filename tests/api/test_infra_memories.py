from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from coreman.core.db.models import Memory, RelayServer
from coreman.core.knowledge.memory import content_hash
from tests.integration.worker_helpers import seed_bot


async def prepare(db_session):
    bot, relay, cipher = await seed_bot(db_session)
    token = "memory-agent-token-synthetic-long"
    relay.agent_token_enc = cipher.encrypt(token, "relay_servers.agent_token_enc")
    await db_session.commit()
    return bot, relay, {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "collect_path,query_path",
    [
        ("/api/infra/memories/collect", "/api/robot/memories/query"),
        ("/api/robot/memories/collect", "/api/infra/memories"),
    ],
)
async def test_collect_current_relay_hash_mtime_and_tombstone(
    client, db_session, collect_path, query_path
):
    bot, relay, headers = await prepare(db_session)
    now = datetime.now(UTC)
    entry = {
        "working_dir": bot.working_dir,
        "file_name": "项目记忆.md",
        "content": "first",
        "file_mtime": (now - timedelta(minutes=2)).timestamp(),
    }
    body = {"server_id": str(relay.id), "memories": [entry]}
    path = collect_path
    result = await client.post(path, json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["created"] == 1
    result = await client.post(path, json=body, headers=headers)
    assert result.json()["data"]["unchanged"] == 1
    row = await db_session.scalar(select(Memory))
    row.content = "admin edit"
    row.content_hash = content_hash(row.content)
    row.file_mtime = now
    await db_session.commit()
    assert (await client.post(path, json=body, headers=headers)).json()["data"]["stale"] == 1
    entry["file_mtime"] = (now + timedelta(seconds=1)).timestamp()
    entry["content"] = "newer local"
    assert (await client.post(path, json=body, headers=headers)).json()["data"]["updated"] == 1
    await db_session.refresh(row)
    row.deleted_at = now
    row.content = ""
    await db_session.commit()
    entry["file_mtime"] = (now + timedelta(seconds=2)).timestamp()
    assert (await client.post(path, json=body, headers=headers)).json()["data"]["deleted"] == 1
    result = await client.get(
        query_path,
        params={"server_id": str(relay.id), "working_dir": bot.working_dir},
        headers=headers,
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["memories"][0]["deleted"] is True
    assert result.json()["data"]["memories"][0]["content"] == ""


async def test_batch_wrong_owner_or_corrupt_file_is_atomic(client, db_session):
    bot, relay, headers = await prepare(db_session)
    entry = {
        "working_dir": bot.working_dir,
        "file_name": "a.md",
        "content": "a",
        "file_mtime": datetime.now(UTC).timestamp(),
    }
    path = "/api/infra/memories/collect"
    body = {
        "server_id": str(relay.id),
        "memories": [entry, entry | {"working_dir": "/unassigned", "file_name": "b.md"}],
    }
    assert (await client.post(path, json=body, headers=headers)).status_code == 409
    assert await db_session.scalar(select(Memory)) is None
    body["memories"] = [entry, entry | {"file_name": "b.md", "content_hash": "0" * 64}]
    assert (await client.post(path, json=body, headers=headers)).status_code == 422
    assert await db_session.scalar(select(Memory)) is None
    body["memories"] = [entry | {"file_name": "../escape.md"}]
    assert (await client.post(path, json=body, headers=headers)).status_code == 422
    other = RelayServer(name="other", host="10.0.0.2", clawrelay_port=9001, model_provider="claude")
    db_session.add(other)
    await db_session.commit()
    body["server_id"] = str(other.id)
    body["memories"] = [entry]
    assert (await client.post(path, json=body, headers=headers)).status_code == 401
    bot.relay_server_id = other.id
    await db_session.commit()
    body["server_id"] = str(relay.id)
    assert (await client.post(path, json=body, headers=headers)).status_code == 409


async def test_workspace_inventory_is_bound_to_relay(client, db_session):
    bot, relay, headers = await prepare(db_session)
    response = await client.get(
        "/api/infra/memories/workspaces", params={"server_id": str(relay.id)}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["working_dirs"] == [bot.working_dir]
    import uuid

    response = await client.get(
        "/api/infra/memories/workspaces", params={"server_id": str(uuid.uuid4())}, headers=headers
    )
    assert response.status_code == 401


async def test_infra_deploy_uses_stored_rows_and_bound_agent(client, db_session, monkeypatch):
    from coreman.core.relay import agent_client

    bot, relay, headers = await prepare(db_session)
    now = datetime.now(UTC)
    db_session.add(
        Memory(
            bot_id=bot.id,
            file_name="stored.md",
            content="stored-content",
            content_hash=content_hash("stored-content"),
            file_mtime=now,
        )
    )
    await db_session.commit()
    calls = []

    async def call(target, cipher, operation, payload=None):
        assert target.id == relay.id
        calls.append(operation)
        if operation == "ping":
            return {"success": True, "memory_protocol": 2}
        assert payload["memories"][0]["content"] == "stored-content"
        return {"success": True, "protocol_version": 2, "count": 1}

    monkeypatch.setattr(agent_client, "call_agent", call)
    body = {"server_id": str(relay.id), "working_dir": bot.working_dir}
    response = await client.post("/api/robot/memories/deploy", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert calls == ["ping", "deploy-memory"]
    assert (
        await client.post(
            "/api/infra/memories/deploy", json=body | {"working_dir": "/other"}, headers=headers
        )
    ).status_code == 409
