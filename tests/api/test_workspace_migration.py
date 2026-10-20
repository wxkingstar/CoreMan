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

    bot, old, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
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
    assert bot.workspace_state == "ready"
    assert bot.memory_snapshot_at is not None
    memory = await db_session.scalar(select(Memory).where(Memory.bot_id == bot.id))
    assert memory.content == "latest memory"


async def test_switch_into_nonempty_unknown_directory_rejected(client, db_session, monkeypatch):
    from coreman.core.bots import workspace_transfer

    bot, _, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
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


async def test_switch_into_directory_marked_by_another_bot_rejected(
    client, db_session, monkeypatch
):
    from coreman.core.bots import workspace_transfer

    bot, _, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    target = await add_node_relay(db_session, name="target")
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))

    async def call(relay, cipher, operation, payload=None):
        if operation == "ping":
            return {"success": True, "workspace_protocol": 1}
        assert operation == "workspace-info"
        return {"success": True, "exists": True, "empty": False, "owned": False, "marked": True}

    monkeypatch.setattr(workspace_transfer, "call_agent", call)
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "existing"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 409, response.text
    assert "其他员工" in response.json()["message"]


async def test_existing_mode_takes_over_unmarked_shared_directory(
    client, db_session, monkeypatch, tmp_path
):
    import os

    from coreman.core.bots import workspace_transfer
    from coreman.core.db.models import RuntimeNode
    from runtime_daemon.agent import Agent, OperationError

    bot, old, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    src_root, dst_root = tmp_path / "source", tmp_path / "skills"
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
    agents[old.id].dispatch(
        {"type": "workspace-init", "working_dir": bot.working_dir, "bot_id": str(bot.id)}
    )
    # 另一套机器人系统的目录：CLAUDE.md 为正本、AGENTS.md 链接过去，没有 CoreMan 所有权标记。
    shared = dst_root / bot.bot_key
    (shared / ".git/info").mkdir(parents=True)
    (shared / "CLAUDE.md").write_text("# 共用规则")
    (shared / "AGENTS.md").symlink_to("CLAUDE.md")
    (shared / "report.csv").write_text("a,b\n")

    async def call(relay, cipher, operation, payload=None):
        try:
            result = agents[relay.id].dispatch({"type": operation, **(payload or {})})
        except OperationError as exc:
            raise workspace_transfer.AgentError(str(exc)) from exc
        assert result["success"], result
        return result

    monkeypatch.setattr(workspace_transfer, "call_agent", call)
    monkeypatch.setattr(memory_transfer, "call_agent", call)
    from coreman.core.relay import agent_client

    monkeypatch.setattr(agent_client, "call_agent", call)
    preview = await client.post(
        f"/api/admin/bots/{bot.id}/workspace/switch-preview",
        json={"relay_server_id": str(target.id), "target_directory": str(shared)},
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()["data"]
    assert data["exists"] and not data["owned"] and data["marked"] is False
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={
            "relay_server_id": str(target.id),
            "workspace_mode": "existing",
            "target_directory": str(shared),
        },
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(bot)
    assert bot.relay_server_id == target.id and bot.working_dir == str(shared)
    assert (shared / "CLAUDE.md").read_text() == "# 共用规则"
    assert os.readlink(shared / "AGENTS.md") == "CLAUDE.md"
    assert (shared / "report.csv").read_text() == "a,b\n"
    assert "/.coreman-workspace.json" in (shared / ".git/info/exclude").read_text()
    info = agents[target.id].dispatch(
        {"type": "workspace-info", "working_dir": str(shared), "bot_id": str(bot.id)}
    )
    assert info["owned"]


async def test_copy_moves_real_files_and_memory_before_binding(
    client, db_session, monkeypatch, tmp_path
):
    import os

    from coreman.core.bots import workspace_transfer
    from coreman.core.db.models import RuntimeNode
    from runtime_daemon.agent import Agent, OperationError

    bot, old, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    src_root, dst_root = tmp_path / "source", tmp_path / "destination"
    src_root.mkdir()
    dst_root.mkdir()
    source_node = await db_session.get(RuntimeNode, old.runtime_node_id)
    source_node.workspace_root = str(src_root)
    bot.working_dir = str(src_root / bot.bot_key)
    target = await add_node_relay(db_session, name="target", workspace_root=str(dst_root))
    from coreman.core.bots import switch_relay as switch_module
    from coreman.core.db.models import BotSkill, Skill, SkillSource

    source_row = SkillSource(key="tools", label="Tools", git_url="https://github.com/example/s.git")
    db_session.add(source_row)
    await db_session.flush()
    skill = Skill(name="query", source_id=source_row.id, enabled=True)
    db_session.add(skill)
    await db_session.flush()
    db_session.add(BotSkill(bot_id=bot.id, skill_id=skill.id, status="installed"))
    await db_session.commit()
    reinstalled = []

    async def reinstall(session, cipher, *, bot, rows, actor):
        reinstalled.append((bot.relay_server_id, [r.skill_id for r in rows], actor.id))

    monkeypatch.setattr(switch_module, "reinstall_after_switch", reinstall)
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
    # 技能不随文件迁移，绑定新实例后按原配置重装。
    assert reinstalled == [(target.id, [skill.id], bot.created_by)]


async def test_copy_replaces_occupied_target_with_git_and_newer_memory(
    client, db_session, monkeypatch, tmp_path
):
    import os
    import time

    from coreman.core.bots import switch_relay as switch_module
    from coreman.core.bots import workspace_transfer
    from coreman.core.db.models import BotSkill, RuntimeNode, Skill, SkillSource
    from runtime_daemon.agent import Agent, OperationError

    bot, old, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    src_root, dst_root = tmp_path / "source", tmp_path / "destination"
    src_root.mkdir()
    dst_root.mkdir()
    source_node = await db_session.get(RuntimeNode, old.runtime_node_id)
    source_node.workspace_root = str(src_root)
    bot.working_dir = str(src_root / bot.bot_key)
    target = await add_node_relay(db_session, name="target", workspace_root=str(dst_root))
    source_row = SkillSource(key="tools", label="Tools", git_url="https://github.com/example/s.git")
    db_session.add(source_row)
    await db_session.flush()
    skills = [Skill(name=name, source_id=source_row.id, enabled=True) for name in "abc"]
    db_session.add_all(skills)
    await db_session.flush()
    db_session.add_all(
        [
            BotSkill(bot_id=bot.id, skill_id=skills[0].id, status="installed"),
            # 之前重装失败、停在失败状态的也要重装；从没装成功的不算。
            BotSkill(bot_id=bot.id, skill_id=skills[1].id, status="failed", installed_at=utcnow()),
            BotSkill(bot_id=bot.id, skill_id=skills[2].id, status="failed"),
        ]
    )
    await db_session.commit()
    reinstalled = []

    async def reinstall(session, cipher, *, bot, rows, actor):
        reinstalled.append(sorted(r.skill_id for r in rows))

    monkeypatch.setattr(switch_module, "reinstall_after_switch", reinstall)
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
    agents[old.id].dispatch(
        {"type": "workspace-init", "working_dir": bot.working_dir, "bot_id": str(bot.id)}
    )
    folder = src_root / bot.bot_key
    (folder / "report.txt").write_text("最新的业务数据")
    (folder / ".git").mkdir()
    (folder / ".git/HEAD").write_text("ref: refs/heads/main\n")
    mem = agents[old.id].memory_dir(bot.working_dir)
    mem.mkdir(parents=True)
    (mem / "remember.md").write_text("记住交付日期")
    # 目标是本员工以前用过的目录：留着旧文件，记忆还比源更新。
    destination = dst_root / bot.bot_key
    agents[target.id].dispatch(
        {"type": "workspace-init", "working_dir": str(destination), "bot_id": str(bot.id)}
    )
    (destination / "stale.txt").write_text("旧文件")
    stale_memory = agents[target.id].memory_dir(str(destination)) / "remember.md"
    stale_memory.parent.mkdir(parents=True)
    stale_memory.write_text("旧的目标记忆")
    later = time.time() + 60
    os.utime(stale_memory, (later, later))

    async def call(relay, cipher, operation, payload=None):
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
    assert bot.relay_server_id == target.id and bot.working_dir == str(destination)
    assert (destination / "report.txt").read_text() == "最新的业务数据"
    assert (destination / ".git/HEAD").read_text() == "ref: refs/heads/main\n"
    assert not (destination / "stale.txt").exists()
    assert stale_memory.read_text() == "记住交付日期"
    assert reinstalled == [sorted([skills[0].id, skills[1].id])]


async def test_copy_never_replaces_directory_of_another_employee(client, db_session, monkeypatch):
    from coreman.core.bots import workspace_transfer

    bot, _, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    target = await add_node_relay(db_session, name="target")
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))

    async def call(relay, cipher, operation, payload=None):
        if operation == "ping":
            return {"success": True, "workspace_protocol": 1, "workspace_replace": True}
        assert operation == "workspace-info"
        return {"success": True, "exists": True, "empty": False, "owned": False, "marked": True}

    monkeypatch.setattr(workspace_transfer, "call_agent", call)
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 409, response.text
    assert "其他员工" in response.json()["message"]


async def test_copy_over_occupied_target_requires_upgraded_runtime(client, db_session, monkeypatch):
    from coreman.core.bots import workspace_transfer

    bot, old, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    target = await add_node_relay(db_session, name="target")
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))

    async def call(relay, cipher, operation, payload=None):
        if operation == "ping":
            return {
                "success": True,
                "memory_protocol": 2,
                "memory_read": True,
                "workspace_protocol": 1,
            }
        if operation == "workspace-info":
            return {"success": True, "exists": True, "empty": False, "owned": True}
        if operation == "read-memory":
            return {"success": True, "memories": []}
        assert operation == "workspace-export"
        return {"success": True, "manifest": []}

    monkeypatch.setattr(workspace_transfer, "call_agent", call)
    monkeypatch.setattr(memory_transfer, "call_agent", call)
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 409, response.text
    assert "升级运行时" in response.json()["message"]
    await db_session.refresh(bot)
    assert bot.relay_server_id == old.id


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

    bot, relay, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    bot.workspace_state = "migrating"
    bot.workspace_deadline = utcnow() + timedelta(minutes=30)
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    version = bot.version
    for endpoint, body in [
        ("switch-relay", {"relay_server_id": str(relay.id), "model": "claude-sonnet-5-5"}),
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

    bot, relay, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
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


async def _real_agents(db_session, tmp_path, monkeypatch, *, shared=False, mount=None):
    """源、目标各一个真实 Agent；平台对节点的调用直接落到它们身上。

    shared=True 时两台节点的项目主目录是同一个目录，相当于挂了同一块共享盘；再给 mount 时目标
    节点的项目主目录是指向它的另一个路径，相当于同一块盘在两台节点挂在不同路径。
    """
    from coreman.core.bots import workspace_transfer
    from coreman.core.db.models import RuntimeNode
    from runtime_daemon.agent import Agent, OperationError

    bot, old, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    src_root = tmp_path / "source"
    dst_root = src_root if shared else tmp_path / "destination"
    src_root.mkdir()
    dst_root.mkdir(exist_ok=True)
    if mount:
        dst_root = tmp_path / mount
        dst_root.symlink_to(src_root)
    source_node = await db_session.get(RuntimeNode, old.runtime_node_id)
    source_node.workspace_root = str(src_root)
    bot.working_dir = str(src_root / bot.bot_key)
    target = await add_node_relay(db_session, name="target", workspace_root=str(dst_root))
    await db_session.commit()
    agents = {
        relay.id: Agent(
            root=root,
            home=tmp_path / home,
            api_url="http://test",
            token="x" * 32,
            relay_id=str(relay.id),
            sessions_dir=tmp_path / home / "relay-sessions",
        )
        for relay, root, home in [(old, src_root, "home1"), (target, dst_root, "home2")]
    }
    seen: list[tuple[str, dict]] = []

    async def call(relay, cipher, operation, payload=None):
        seen.append((operation, payload or {}))
        try:
            result = agents[relay.id].dispatch({"type": operation, **(payload or {})})
        except OperationError as exc:
            raise workspace_transfer.AgentError(
                "Agent 未完成操作", code="operation_failed", detail=str(exc)
            ) from exc
        assert result["success"], result
        return result

    monkeypatch.setattr(workspace_transfer, "call_agent", call)
    monkeypatch.setattr(memory_transfer, "call_agent", call)
    agents[old.id].dispatch(
        {"type": "workspace-init", "working_dir": bot.working_dir, "bot_id": str(bot.id)}
    )
    return bot, old, target, agents, seen


def _git(cwd, *args):
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def test_switch_reclones_repo_and_keeps_claude_session(
    client, db_session, monkeypatch, tmp_path
):
    import runtime_daemon.agent as agent_module
    from coreman.core.chat import sessions

    bot, old, target, agents, seen = await _real_agents(db_session, tmp_path, monkeypatch)
    url = "https://example.com/team/app.git"
    bare, seed = tmp_path / "remote.git", tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "app.py").write_text("print(1)\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    home = tmp_path / "user-home"
    home.mkdir()
    (home / ".gitconfig").write_text(f'[url "file://{bare}"]\n\tinsteadOf = {url}\n')
    monkeypatch.setenv("HOME", str(home))
    original = agent_module.run_command
    monkeypatch.setattr(
        agent_module,
        "run_command",
        lambda command, *a, **kw: original(
            [c.replace("allow=never", "allow=always") for c in command], *a, **kw
        ),
    )
    repo = tmp_path / "source" / bot.bot_key / "app"
    _git(tmp_path, "clone", "-q", str(bare), str(repo))
    _git(repo, "remote", "set-url", "origin", url)
    (repo / "app.py").write_text("print(2)\n")
    info = await sessions.get_or_create(
        db_session,
        bot_id=bot.id,
        session_key="u1",
        backend="claude",
        ttl_hours=72,
        speaker_user_id=None,
    )
    sid = str(info.relay_session_id)
    project = agents[old.id].claude_project_dir(bot.working_dir)
    project.mkdir(parents=True)
    (project / f"{sid}.jsonl").write_text('{"type":"user"}\n')
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(bot)
    moved = tmp_path / "destination" / bot.bot_key / "app"
    assert (moved / "app.py").read_text() == "print(2)\n"
    assert _git(moved, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
    operations = [op for op, _ in seen]
    assert "workspace-clone" in operations
    manifest = next(p["manifest"] for op, p in seen if op == "workspace-import" and "manifest" in p)
    assert not any(e["path"].startswith("app/.git/") for e in manifest)
    # Claude 会话文件到了新目录，会话映射原样保留：下一轮直接续聊。
    assert (agents[target.id].claude_project_dir(bot.working_dir) / f"{sid}.jsonl").exists()
    (row,) = await sessions.list_for_bot(db_session, bot.id)
    await db_session.refresh(row)
    assert str(row.relay_session_id) == sid and row.inherited_from is None


async def test_switch_falls_back_to_full_copy_when_clone_fails(
    client, db_session, monkeypatch, tmp_path
):
    bot, old, target, agents, seen = await _real_agents(db_session, tmp_path, monkeypatch)
    repo = tmp_path / "source" / bot.bot_key / "app"
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    (repo / "app.py").write_text("print(1)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", "https://example.com/team/unreachable.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 200, response.text
    moved = tmp_path / "destination" / bot.bot_key / "app"
    assert _git(moved, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD")
    exports = [p for op, p in seen if op == "workspace-export"]
    assert exports[-1]["expand"] == ["app"]


async def test_switch_failure_reports_runtime_reason(client, db_session, monkeypatch, tmp_path):
    from runtime_daemon import workspace

    bot, old, target, agents, seen = await _real_agents(db_session, tmp_path, monkeypatch)
    monkeypatch.setattr(workspace, "MAX_FILES", 1)
    folder = tmp_path / "source" / bot.bot_key
    (folder / "a.txt").write_text("a")
    (folder / "b.txt").write_text("b")
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 502, response.text
    assert "超过迁移上限" in response.json()["message"]
    await db_session.refresh(bot)
    assert bot.relay_server_id == old.id and "超过迁移上限" in bot.workspace_error


async def _claim(db_session):
    from coreman.core.db.models import ProcessInstance

    if await db_session.get(ProcessInstance, "worker:migration") is None:
        now = utcnow()
        db_session.add(
            ProcessInstance(
                id="worker:migration",
                service="worker",
                version="test",
                started_at=now,
                heartbeat_at=now,
                capacity=2,
            )
        )
        await db_session.commit()
    claimed = await tasks.claim(db_session, lane="normal", instance_id="worker:migration")
    await db_session.commit()
    return claimed


async def _failed_switch(client, db_session, monkeypatch, tmp_path):
    """真实节点上换机失败（源目录文件数超过上限），原绑定保留。"""
    from runtime_daemon import workspace

    bot, old, target, agents, seen = await _real_agents(db_session, tmp_path, monkeypatch)
    limit = workspace.MAX_FILES
    monkeypatch.setattr(workspace, "MAX_FILES", 1)
    folder = tmp_path / "source" / bot.bot_key
    (folder / "a.txt").write_text("a")
    (folder / "b.txt").write_text("b")
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 502, response.text
    monkeypatch.setattr(workspace, "MAX_FILES", limit)
    await db_session.refresh(bot)
    return bot, old, target


async def test_failed_migration_keeps_employee_working_on_original_runtime(
    client, db_session, monkeypatch, tmp_path
):
    """迁移失败、原绑定已保留：任务照常认领，失败原因留在工作目录面板，目标不再被预留。"""
    bot, old, _ = await _failed_switch(client, db_session, monkeypatch, tmp_path)
    assert bot.relay_server_id == old.id and bot.working_dir.startswith(str(tmp_path / "source"))
    assert bot.workspace_state == "ready"
    assert bot.workspace_phase == "迁移失败，原绑定已保留"
    assert "超过迁移上限" in bot.workspace_error
    assert bot.workspace_deadline is None and bot.workspace_operation_id is None
    assert bot.workspace_target_relay_id is None and bot.workspace_target_dir is None
    panel = (await client.get(f"/api/admin/bots/{bot.id}/workspace")).json()["data"]
    assert panel["state"] == "ready" and panel["phase"] == "迁移失败，原绑定已保留"
    assert panel["error"] == bot.workspace_error
    queued = await tasks.enqueue(db_session, tasks.NewTask(bot_id=bot.id, kind="chat"))
    await db_session.commit()
    claimed = await _claim(db_session)
    assert claimed is not None and claimed.id == queued.id


async def test_retry_after_failed_migration_switches_and_clears_error(
    client, db_session, monkeypatch, tmp_path
):
    bot, _, target = await _failed_switch(client, db_session, monkeypatch, tmp_path)
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(bot)
    assert bot.relay_server_id == target.id
    assert (tmp_path / "destination" / bot.bot_key / "b.txt").read_text() == "b"
    assert bot.workspace_state == "ready"
    assert bot.workspace_error is None and bot.workspace_phase is None
    assert bot.workspace_target_relay_id is None and bot.workspace_target_dir is None
    queued = await tasks.enqueue(db_session, tasks.NewTask(bot_id=bot.id, kind="chat"))
    await db_session.commit()
    claimed = await _claim(db_session)
    assert claimed is not None and claimed.id == queued.id


async def test_initialize_failure_still_fences_tasks_until_retry(client, db_session, monkeypatch):
    """初始化失败是原目录本身不可用（如工作说明冲突），和迁移失败不同：任务不认领，直到重试成功。"""
    from coreman.core.bots import workspace_transfer

    bot, _, _ = await seed_bot(db_session, model="claude-sonnet-5-5")
    await db_session.commit()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))

    async def conflict(relay, cipher, operation, payload=None):
        if operation == "ping":
            return {"success": True, "workspace_protocol": 1}
        raise workspace_transfer.AgentError("Agent 未完成操作", code="instructions_conflict")

    monkeypatch.setattr(workspace_transfer, "call_agent", conflict)
    url = f"/api/admin/bots/{bot.id}/workspace/initialize"
    assert (await client.post(url)).status_code == 409
    await db_session.refresh(bot)
    assert bot.workspace_state == "failed" and "AGENTS.md" in bot.workspace_error
    queued = await tasks.enqueue(db_session, tasks.NewTask(bot_id=bot.id, kind="chat"))
    await db_session.commit()
    assert await _claim(db_session) is None

    async def merged(relay, cipher, operation, payload=None):
        return {"success": True, "workspace_protocol": 1, "initialized": True}

    monkeypatch.setattr(workspace_transfer, "call_agent", merged)
    assert (await client.post(url)).status_code == 200
    await db_session.refresh(bot)
    assert bot.workspace_state == "ready" and bot.workspace_error is None
    claimed = await _claim(db_session)
    assert claimed is not None and claimed.id == queued.id


def _untransferred_files(folder):
    """换机复制不带的东西：放在共享盘上的这些文件，换机后必须原样还在。"""
    (folder / ".env").write_text("TOKEN=keep")
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "dep.js").write_text("module.exports = 1")
    (folder / ".coreman-worktrees" / "s1").mkdir(parents=True)
    (folder / ".coreman-worktrees" / "s1" / "wip.py").write_text("print(1)")
    (folder / "report.txt").write_text("业务数据")
    return [".env", "node_modules/dep.js", ".coreman-worktrees/s1/wip.py", "report.txt"]


async def test_switch_over_shared_storage_skips_copy_even_when_asked_to_copy(
    client, db_session, monkeypatch, tmp_path
):
    bot, old, target, agents, seen = await _real_agents(
        db_session, tmp_path, monkeypatch, shared=True
    )
    folder = tmp_path / "source" / bot.bot_key
    kept = {name: (folder / name).read_text() for name in _untransferred_files(folder)}
    mem = agents[old.id].memory_dir(bot.working_dir)
    mem.mkdir(parents=True)
    (mem / "remember.md").write_text("记住交付日期")
    from coreman.core.bots import workspace_transfer
    from coreman.core.relay import agent_client

    monkeypatch.setattr(agent_client, "call_agent", workspace_transfer.call_agent)
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    preview = await client.post(
        f"/api/admin/bots/{bot.id}/workspace/switch-preview",
        json={"relay_server_id": str(target.id)},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["shared"] is True
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(bot)
    assert bot.relay_server_id == target.id and bot.working_dir == str(folder)
    assert {name: (folder / name).read_text() for name in kept} == kept
    operations = [op for op, _ in seen]
    assert "workspace-probe" in operations and "workspace-export" not in operations
    assert not list(folder.glob(".coreman-probe-*"))
    # 记忆仍在各节点本地，照常回收并部署到目标节点。
    assert (agents[target.id].memory_dir(bot.working_dir) / "remember.md").read_text() == (
        "记住交付日期"
    )


async def test_rate_limit_switch_default_mode_skips_copy_on_shared_storage(
    db_session, monkeypatch, tmp_path
):
    """限流卡片的自动切换不带处理方式（默认复制），共享盘上同样不能复制。"""
    from coreman.core.bots.switch_relay import switch_relay

    bot, old, target, agents, seen = await _real_agents(
        db_session, tmp_path, monkeypatch, shared=True
    )
    folder = tmp_path / "source" / bot.bot_key
    kept = {name: (folder / name).read_text() for name in _untransferred_files(folder)}
    result = await switch_relay(
        db_session,
        bot=bot,
        target=target,
        model=None,
        actor=await db_session.get(User, bot.created_by),
        member_ids=set(),
        ip=None,
    )
    assert result.new_relay_id == target.id
    assert {name: (folder / name).read_text() for name in kept} == kept
    assert "workspace-export" not in [op for op, _ in seen]


async def test_switch_stops_when_shared_storage_cannot_be_confirmed(
    client, db_session, monkeypatch, tmp_path
):
    """探测出错不能当成不共享去复制：停下换机、保留原绑定，文件不动。"""
    from coreman.core.bots import workspace_transfer

    bot, old, target, agents, seen = await _real_agents(
        db_session, tmp_path, monkeypatch, shared=True
    )
    folder = tmp_path / "source" / bot.bot_key
    kept = {name: (folder / name).read_text() for name in _untransferred_files(folder)}
    real = workspace_transfer.call_agent

    async def flaky(relay, cipher, operation, payload=None):
        if operation == "workspace-probe" and (payload or {}).get("action") == "check":
            raise workspace_transfer.AgentError("Agent 连接失败（ReadTimeout）")
        return await real(relay, cipher, operation, payload)

    monkeypatch.setattr(workspace_transfer, "call_agent", flaky)
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 502, response.text
    assert "无法确认目标目录是否与当前目录共享" in response.json()["message"]
    await db_session.refresh(bot)
    assert bot.relay_server_id == old.id
    assert {name: (folder / name).read_text() for name in kept} == kept
    assert "workspace-export" not in [op for op, _ in seen]
    assert not list(folder.glob(".coreman-probe-*"))


async def test_switch_skips_copy_when_shared_storage_is_mounted_at_another_path(
    client, db_session, monkeypatch, tmp_path
):
    """同一块盘在源节点挂在 source、目标节点挂在 mnt-b：路径不同也要认出是同一份目录。"""
    bot, old, target, agents, seen = await _real_agents(
        db_session, tmp_path, monkeypatch, shared=True, mount="mnt-b"
    )
    folder = tmp_path / "source" / bot.bot_key
    kept = {name: (folder / name).read_text() for name in _untransferred_files(folder)}
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(bot)
    assert bot.relay_server_id == target.id
    assert bot.working_dir == str(tmp_path / "mnt-b" / bot.bot_key)
    assert {name: (folder / name).read_text() for name in kept} == kept
    assert "workspace-export" not in [op for op, _ in seen]
    assert not list(folder.glob(".coreman-probe-*"))


async def test_switch_skips_copy_on_shared_storage_after_marker_was_cleaned(
    client, db_session, monkeypatch, tmp_path
):
    """归属标记被 git clean -x 之类删掉后，同一份目录看起来无人认领，复制前仍要先探测。"""
    bot, old, target, agents, seen = await _real_agents(
        db_session, tmp_path, monkeypatch, shared=True
    )
    folder = tmp_path / "source" / bot.bot_key
    kept = {name: (folder / name).read_text() for name in _untransferred_files(folder)}
    (folder / ".coreman-workspace.json").unlink()
    await login_existing(client, db_session, await db_session.get(User, bot.created_by))
    response = await client.post(
        f"/api/admin/bots/{bot.id}/switch-relay",
        json={"relay_server_id": str(target.id), "workspace_mode": "copy"},
        headers={"If-Match": str(bot.version)},
    )
    assert response.status_code == 200, response.text
    assert {name: (folder / name).read_text() for name in kept} == kept
    assert "workspace-export" not in [op for op, _ in seen]


async def test_shared_directory_needs_both_runtimes_and_cleans_up_on_the_source(
    db_session, monkeypatch
):
    """旧运行时不支持探测按不共享处理；探测文件不管找没找到都由源节点删除。"""
    from coreman.core.bots import workspace_transfer

    bot, old, _ = await seed_bot(db_session)
    target = await add_node_relay(db_session, name="target")
    calls: list[tuple[str, str | None, str | None]] = []
    capable = {old.id: True, target.id: True}
    found = {"value": False}

    async def call(relay, cipher, operation, payload=None):
        payload = payload or {}
        side = "source" if relay.id == old.id else "target"
        calls.append((operation, payload.get("action"), side))
        if operation == "ping":
            return {"workspace_probe": 1} if capable[relay.id] else {}
        return {"found": found["value"]} if payload.get("action") == "check" else {}

    monkeypatch.setattr(workspace_transfer, "call_agent", call)
    probes = [
        ("workspace-probe", "create", "source"),
        ("workspace-probe", "check", "target"),
        ("workspace-probe", "remove", "source"),
    ]
    assert not await workspace_transfer.shared_directory(bot, old, target, "/elsewhere", None)
    assert [c for c in calls if c[0] == "workspace-probe"] == probes
    calls.clear()
    found["value"] = True
    assert await workspace_transfer.shared_directory(bot, old, target, "/elsewhere", None)
    assert [c for c in calls if c[0] == "workspace-probe"] == probes
    calls.clear()
    capable[target.id] = False
    assert not await workspace_transfer.shared_directory(bot, old, target, bot.working_dir, None)
    assert all(op == "ping" for op, _, _ in calls)
