from datetime import timedelta

import pytest
from sqlalchemy import select

from coreman.core.db.models import Bot, Memory, User
from coreman.core.knowledge import memory_transfer
from coreman.core.knowledge.memory import content_hash
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_existing
from tests.fakes.runtime_node import add_node_relay, attach_node
from tests.integration.worker_helpers import seed_bot


@pytest.fixture(autouse=True)
def workspace_channel(monkeypatch):
    # These tests exercise real memory collection/deployment; only the independent
    # file channel is replaced. Workspace transfer itself has separate E2E tests.
    from coreman.core.bots import workspace_transfer

    async def call(relay, cipher, operation, payload=None):
        if operation == "ping":
            return {"success": True, "workspace_protocol": 1}
        if operation == "workspace-info":
            return {"success": True, "exists": False, "empty": True, "owned": False}
        if operation == "workspace-export":
            return {"success": True, "manifest": [], "excluded": []}
        return {"success": True}

    monkeypatch.setattr(workspace_transfer, "call_agent", call)


async def prepare(client, session):
    bot, old, _ = await seed_bot(session)
    await attach_node(session, old)
    new = await add_node_relay(session, name="target")
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
        (old_id, "ping"),
        (old_id, "read-memory"),
        (new_id, "ping"),
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


async def test_codex_target_without_shared_memory_is_refused_only_when_memory_exists(db_session):
    from coreman.core.errors import ApiError

    bot, _, _ = await seed_bot(db_session)
    codex = await add_node_relay(db_session, name="codex", provider="codex")
    claude = await add_node_relay(db_session, name="claude")
    old_runtime = {"success": True, "memory_protocol": 2}
    # 没有记忆时 Codex 读不到也不缺什么。
    await memory_transfer.require_shared_layout(db_session, bot, codex, old_runtime)
    db_session.add(
        Memory(
            bot_id=bot.id,
            file_name="gone.md",
            content="",
            content_hash=content_hash(""),
            deleted_at=utcnow(),
        )
    )
    await db_session.flush()
    await memory_transfer.require_shared_layout(db_session, bot, codex, old_runtime)
    db_session.add(
        Memory(
            bot_id=bot.id,
            file_name="kept.md",
            content="记住交付日期",
            content_hash=content_hash("记住交付日期"),
        )
    )
    await db_session.flush()
    with pytest.raises(ApiError, match="Codex 读不到"):
        await memory_transfer.require_shared_layout(db_session, bot, codex, old_runtime)
    await memory_transfer.require_shared_layout(
        db_session, bot, codex, {**old_runtime, "memory_layout": 2}
    )
    # Claude 目标照旧部署到原位置。
    await memory_transfer.require_shared_layout(db_session, bot, claude, old_runtime)


async def test_codex_prompt_names_the_memory_directory_only_on_capable_runtimes(db_session):
    from coreman.core.db.models import RuntimeNode
    from coreman.core.knowledge import memory

    codex = await add_node_relay(db_session, name="codex", provider="codex")
    node = await db_session.get(RuntimeNode, codex.runtime_node_id)
    assert await memory.prompt_dir(db_session, codex, "codex", "/srv/w/sales") == ""
    node.capabilities = {"codex": {"memory_layout_v1": True}}
    await db_session.flush()
    assert (
        await memory.prompt_dir(db_session, codex, "codex", "/srv/w/sales")
        == "/srv/w/sales/.coreman-memory"
    )
    assert await memory.prompt_dir(db_session, codex, "claude", "/srv/w/sales") == ""
