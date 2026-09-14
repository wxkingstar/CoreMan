from datetime import timedelta

from sqlalchemy import select

from coreman.core.db.models import Bot, Memory, RelayServer, User
from coreman.core.knowledge import memory_transfer
from coreman.core.knowledge.memory import content_hash
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_existing
from tests.integration.worker_helpers import seed_bot


async def prepare(client, session):
    bot, old, cipher = await seed_bot(session)
    old.agent_port = 9000
    old.agent_token_enc = cipher.encrypt("synthetic-old-token", "relay_servers.agent_token_enc")
    new = RelayServer(
        name="target",
        host="new.test",
        clawrelay_port=80,
        model_provider="claude",
        agent_port=9000,
        agent_token_enc=cipher.encrypt("synthetic-new-token", "relay_servers.agent_token_enc"),
    )
    session.add(new)
    session.add(
        Memory(
            bot_id=bot.id,
            file_name="deleted.md",
            content="",
            content_hash=content_hash(""),
            file_mtime=utcnow(),
            deleted_at=utcnow(),
        )
    )
    await session.commit()
    await login_existing(client, session, await session.get(User, bot.created_by))
    return bot, old, new


async def test_switch_collects_then_deploys_before_rebind(client, db_session, monkeypatch):
    bot, old, new = await prepare(client, db_session)
    bot_id, old_id, new_id = bot.id, old.id, new.id
    operations = []

    async def call(relay, cipher, operation, payload=None):
        operations.append((relay.id, operation))
        if operation == "ping":
            return {"success": True, "memory_protocol": 2, "memory_read": True}
        if operation == "read-memory":
            return {
                "success": True,
                "memories": [
                    {
                        "working_dir": bot.working_dir,
                        "file_name": "latest.md",
                        "content": "new memory",
                        "file_mtime": (utcnow() - timedelta(seconds=1)).timestamp(),
                    }
                ],
            }
        assert operation == "deploy-memory"
        assert (await db_session.get(Bot, bot_id)).relay_server_id == old_id
        assert [row["file_name"] for row in payload["memories"]] == ["deleted.md", "latest.md"]
        assert payload["memories"][0]["deleted"] is True
        return {"success": True, "protocol_version": 2, "count": 2}

    monkeypatch.setattr(memory_transfer, "call_agent", call)
    result = await client.post(
        f"/api/admin/bots/{bot_id}/switch-relay",
        json={"relay_server_id": str(new_id)},
        headers={"If-Match": str(bot.version)},
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["memory_status"] == "transferred"
    assert operations == [
        (new_id, "ping"),
        (old_id, "ping"),
        (old_id, "read-memory"),
        (new_id, "deploy-memory"),
    ]
    await db_session.refresh(bot)
    assert bot.relay_server_id == new_id
    assert (
        await db_session.scalar(select(Memory).where(Memory.file_name == "latest.md"))
    ).content == "new memory"


async def test_switch_deploy_failure_keeps_original_binding(client, db_session, monkeypatch):
    bot, old, new = await prepare(client, db_session)
    old_id = old.id

    async def call(relay, cipher, operation, payload=None):
        if operation == "ping":
            return {"success": True, "memory_protocol": 2, "memory_read": True}
        if operation == "read-memory":
            return {"success": True, "memories": []}
        raise memory_transfer.AgentError("simulated failure")

    monkeypatch.setattr(memory_transfer, "call_agent", call)
    result = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(new.id)},
        headers={"If-Match": str(bot.version)},
    )
    assert result.status_code == 502
    await db_session.refresh(bot)
    assert bot.relay_server_id == old_id
    assert await db_session.scalar(select(Memory.id).where(Memory.bot_id == bot.id)) is not None


async def test_switch_rejects_target_directory_owned_by_another_bot(
    client, db_session, monkeypatch
):
    bot, _, new = await prepare(client, db_session)
    occupied = Bot(
        bot_key="other",
        platform="wecom",
        name="other",
        created_by=bot.created_by,
        relay_server_id=new.id,
        working_dir=bot.working_dir,
        credentials_enc="",
        model=bot.model,
    )
    db_session.add(occupied)
    await db_session.commit()

    async def call(*args):
        raise AssertionError("must not access another bot's files")

    monkeypatch.setattr(memory_transfer, "call_agent", call)
    result = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(new.id)},
        headers={"If-Match": str(bot.version)},
    )
    assert result.status_code == 409
