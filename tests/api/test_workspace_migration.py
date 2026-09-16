"""Workspace migration fences execution and retains recovered memory on failure."""

from datetime import timedelta

from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.db.models import Memory, User
from coreman.core.knowledge import memory_transfer
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_existing
from tests.fakes.runtime_node import add_node_relay
from tests.integration.worker_helpers import seed_bot


async def test_pending_workspace_is_not_claimed(db_session):
    bot, _, _ = await seed_bot(db_session)
    bot.workspace_state = "pending"
    await tasks.enqueue(db_session, tasks.NewTask(bot_id=bot.id, kind="chat"))
    await db_session.commit()
    assert await tasks.claim(db_session, lane="normal", instance_id="test") is None


async def test_migration_retains_collected_memory_when_copy_fails(client, db_session, monkeypatch):
    from coreman.core.bots import workspace_transfer

    bot, old, _ = await seed_bot(db_session, model="claude-sonnet-4-6")
    target = await add_node_relay(db_session, name="target")
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    original_id = old.id

    async def call(relay, cipher, operation, payload=None):
        if operation == "ping":
            return {
                "success": True,
                "memory_protocol": 2,
                "memory_read": True,
                "workspace_protocol": 1,
            }
        if operation == "workspace-info":
            return {"success": True, "exists": False, "empty": True, "owned": False}
        if operation == "read-memory":
            return {
                "success": True,
                "memories": [
                    {
                        "working_dir": bot.working_dir,
                        "file_name": "new.md",
                        "content": "latest memory",
                        "file_mtime": (utcnow() - timedelta(seconds=1)).timestamp(),
                    }
                ],
            }
        if operation == "workspace-export":
            raise workspace_transfer.AgentError("source interrupted")
        raise AssertionError(operation)

    monkeypatch.setattr(workspace_transfer, "call_agent", call)
    monkeypatch.setattr(memory_transfer, "call_agent", call)
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 502, response.text
    await db_session.refresh(bot)
    assert bot.relay_server_id == original_id
    assert bot.workspace_state == "failed"
    assert bot.memory_snapshot_at is not None
    memory = await db_session.scalar(select(Memory).where(Memory.bot_id == bot.id))
    assert memory.content == "latest memory"


async def test_switch_into_nonempty_unknown_directory_rejected(client, db_session, monkeypatch):
    from coreman.core.bots import workspace_transfer

    bot, _, _ = await seed_bot(db_session, model="claude-sonnet-4-6")
    target = await add_node_relay(db_session, name="target")
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))

    async def call(relay, cipher, operation, payload=None):
        if operation == "ping":
            return {"success": True, "workspace_protocol": 1}
        assert operation == "workspace-info"
        return {"success": True, "exists": True, "empty": False, "owned": False}

    monkeypatch.setattr(workspace_transfer, "call_agent", call)
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "existing"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 409, response.text


async def test_copy_moves_real_files_and_memory_before_binding(
    client, db_session, monkeypatch, tmp_path
):
    import os

    from coreman.core.bots import workspace_transfer
    from coreman.core.db.models import RuntimeNode
    from runtime_daemon.agent import Agent, OperationError

    bot, old, _ = await seed_bot(db_session, model="claude-sonnet-4-6")
    src_root, dst_root = tmp_path / "source", tmp_path / "destination"
    src_root.mkdir()
    dst_root.mkdir()
    source_node = await db_session.get(RuntimeNode, old.runtime_node_id)
    source_node.workspace_root = str(src_root)
    bot.working_dir = str(src_root / bot.bot_key)
    target = await add_node_relay(db_session, name="target", workspace_root=str(dst_root))
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    agents = {
        relay.id: Agent(
            root=root,
            home=tmp_path / home,
            api_url="http://test",
            token="x" * 32,
            relay_id=str(relay.id),
        )
        for relay, root, home in [(old, src_root, "home1"), (target, dst_root, "home2")]
    }
    source = agents[old.id]
    source.dispatch(
        {
            "type": "workspace-init",
            "working_dir": bot.working_dir,
            "bot_id": str(bot.id),
            "content": "# My employee",
        }
    )
    folder = src_root / bot.bot_key
    (folder / "report.txt").write_text("尚未提交的业务数据" * 50000)
    (folder / "empty.txt").write_text("")
    (folder / "latest.txt").symlink_to("report.txt")
    mem = source.memory_dir(bot.working_dir)
    mem.mkdir(parents=True)
    (mem / "remember.md").write_text("记住交付日期")
    seen = []

    async def call(relay, cipher, operation, payload=None):
        seen.append(operation)
        if operation == "deploy-memory":
            await db_session.refresh(bot)
            assert bot.relay_server_id == old.id
        try:
            result = agents[relay.id].dispatch({"type": operation, **(payload or {})})
        except OperationError as exc:
            raise workspace_transfer.AgentError(str(exc)) from exc
        assert result["success"], result
        return result

    monkeypatch.setattr(workspace_transfer, "call_agent", call)
    monkeypatch.setattr(memory_transfer, "call_agent", call)
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(bot)
    destination = dst_root / bot.bot_key
    assert bot.relay_server_id == target.id and bot.workspace_state == "ready"
    assert (destination / "report.txt").read_bytes() == (folder / "report.txt").read_bytes()
    assert (destination / "empty.txt").read_bytes() == b""
    assert os.readlink(destination / "CLAUDE.md") == "AGENTS.md"
    assert os.readlink(destination / "latest.txt") == "report.txt"
    assert (
        agents[target.id].memory_dir(str(destination)) / "remember.md"
    ).read_text() == "记住交付日期"
    assert seen.index("read-memory") < seen.index("workspace-export") < seen.index("deploy-memory")
    assert folder.exists()


async def test_migrating_destination_reservation_blocks_overlapping_new_workspace(db_session):
    import pytest

    from coreman.core.bots.workspace import reserve_workspace
    from coreman.core.errors import ApiError

    bot, _, _ = await seed_bot(db_session)
    target = await add_node_relay(db_session, name="target", workspace_root="/home/ai")
    bot.workspace_state = "migrating"
    bot.workspace_target_relay_id = target.id
    bot.workspace_target_dir = "/home/ai/parent"
    await db_session.commit()
    with pytest.raises(ApiError, match="重叠"):
        await reserve_workspace(db_session, target.id, "/home/ai/parent/child")


async def test_model_only_switch_and_toggle_cannot_interrupt_migration(client, db_session):
    from datetime import timedelta

    bot, relay, _ = await seed_bot(db_session, model="claude-sonnet-4-6")
    bot.workspace_state = "migrating"
    bot.workspace_deadline = utcnow() + timedelta(minutes=30)
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    version = bot.version
    for endpoint, body in [
        ("switch-relay", {"relay_server_id": str(relay.id), "model": "claude-sonnet-4-6"}),
        ("toggle", {}),
    ]:
        response = await client.post(
            f"/api/admin/bots/{bot.id}/{endpoint}", json=body, headers={"If-Match": str(version)}
        )
        assert response.status_code == 409, response.text
    await db_session.refresh(bot)
    assert bot.version == version and bot.workspace_state == "migrating"


async def test_expired_same_runtime_directory_migration_can_retry(client, db_session, monkeypatch):
    from coreman.core.bots import switch_relay

    bot, relay, _ = await seed_bot(db_session, model="claude-sonnet-4-6")
    bot.workspace_state = "migrating"
    bot.workspace_deadline = utcnow() - timedelta(minutes=1)
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))

    async def prepared(*args, **kwargs):
        assert kwargs["directory"] == "/data/skills/recovered"
        return "transferred"

    monkeypatch.setattr(switch_relay, "prepare_switch", prepared)
    result = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(relay.id), "target_directory": "/data/skills/recovered"},
        headers={"If-Match": str(bot.version)},
    )
    assert result.status_code == 200, result.text
