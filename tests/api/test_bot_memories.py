from sqlalchemy import select

from coreman.core.db.models import AuditLog, User
from tests.api.conftest import login_as, login_existing
from tests.api.test_infra_memories import prepare


async def test_memory_crud_scope_version_and_tombstone(client, db_session):
    bot, _, _ = await prepare(db_session)
    actor = await db_session.get(User, bot.created_by)
    await login_existing(client, db_session, actor)
    base = f"/api/admin/bots/{bot.id}/memories"
    body = {"file_name": "policy.md", "content": "PRIVATE-MEMORY-CONTENT"}
    result = await client.post(base, json=body)
    assert result.status_code == 200, result.text
    row = result.json()["data"]
    assert row["content"] == body["content"] and row["version"] == 1
    path = f"{base}/{row['id']}"
    result = await client.get(base)
    assert "content" not in result.json()["data"][0]
    assert (await client.put(path, json=body)).status_code == 428
    assert (
        await client.put(path, json=body | {"content": "edited"}, headers={"If-Match": "1"})
    ).status_code == 200
    assert (await client.delete(path, headers={"If-Match": "1"})).status_code == 409
    result = await client.delete(path, headers={"If-Match": "2"})
    assert result.status_code == 200
    assert (await client.get(base)).json()["data"] == []
    assert (await client.get(path)).json()["data"]["content"] == ""
    assert (await client.put(path, json=body, headers={"If-Match": "3"})).status_code == 200
    audits = list(await db_session.scalars(select(AuditLog)))
    assert "PRIVATE-MEMORY-CONTENT" not in str([row.diff for row in audits])
    await login_as(client, db_session, role="platform_admin")
    for route in (base, path):
        assert (await client.get(route)).status_code == 403
    assert (await client.post(f"{base}/collect")).status_code == 403
    assert (await client.post(f"{base}/deploy")).status_code == 403


async def test_deploy_preflight_stops_old_agent_before_writes(client, db_session, monkeypatch):
    from coreman.api.routers import bot_memories

    bot, _, _ = await prepare(db_session)
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    base = f"/api/admin/bots/{bot.id}/memories"
    result = await client.post(base, json={"file_name": "note.md", "content": "note"})
    row = result.json()["data"]
    await client.delete(f"{base}/{row['id']}", headers={"If-Match": "1"})
    operations = []

    async def old_agent(relay, cipher, operation, payload=None):
        operations.append(operation)
        return {"success": True}

    monkeypatch.setattr(bot_memories, "call_agent", old_agent)
    assert (await client.post(f"{base}/deploy")).status_code == 409
    assert operations == ["ping"]

    async def current_agent(relay, cipher, operation, payload=None):
        operations.append(operation)
        if operation == "ping":
            return {"success": True, "memory_protocol": 2}
        assert payload["memories"][0]["deleted"] is True
        assert payload["memories"][0]["content"] == ""
        return {"success": True, "protocol_version": 2, "count": 1}

    monkeypatch.setattr(bot_memories, "call_agent", current_agent)
    result = await client.post(f"{base}/deploy")
    assert result.status_code == 200, result.text
    assert operations == ["ping", "ping", "deploy-memory"]
