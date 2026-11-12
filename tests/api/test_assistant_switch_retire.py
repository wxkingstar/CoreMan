"""助理换运行时之后旧目录登记待删，看护进程经节点删掉它；原目录照常在用时不登记；普通员工不变。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from coreman.core.assistants import retired_dirs
from coreman.core.db.models import AssistantRetiredDir, RelayServer, User
from coreman.core.db.session import make_session_factory
from tests.api.conftest import login_existing
from tests.api.test_workspace_migration import _assistant_agents, _real_agents, _switch


async def test_switching_away_retires_the_old_directory_and_the_node_removes_it(
    client, db_session, monkeypatch, tmp_path, db_engine
):
    bot, old, target, agents, seen = await _assistant_agents(
        client, db_session, tmp_path, monkeypatch
    )
    old_dir = bot.working_dir
    response = await _switch(client, bot, target)
    assert response.status_code == 200, response.text
    row = await db_session.scalar(select(AssistantRetiredDir))
    assert row is not None and row.relay_server_id == old.id and row.working_dir == old_dir
    assert row.bot_id == bot.id

    calls: list[str] = []

    async def call(relay, cipher, operation, payload=None):
        calls.append(operation)
        return agents[relay.id].dispatch({"type": operation, **(payload or {})})

    monkeypatch.setattr(retired_dirs, "call_agent", call)
    agents[old.id].assistant_only = True  # 助理专用实例才接 workspace-remove
    counts = await retired_dirs.tick(make_session_factory(db_engine), None, datetime.now(UTC))
    assert counts["assistant_dirs_removed"] == 1 and calls == ["workspace-remove"]
    for _ in range(100):
        if not (tmp_path / "source" / bot.bot_key).exists():
            break
        await asyncio.sleep(0.05)
    assert not (tmp_path / "source" / bot.bot_key).exists()
    await db_session.refresh(bot)
    # 新目录不受影响。
    assert (tmp_path / "destination" / bot.bot_key / "AGENTS.md").exists()
    assert await db_session.scalar(select(AssistantRetiredDir)) is None


async def test_rebinding_on_the_same_node_retires_nothing(
    client, db_session, monkeypatch, tmp_path
):
    bot, old, _, agents, _ = await _assistant_agents(client, db_session, tmp_path, monkeypatch)
    second = RelayServer(
        runtime_node_id=old.runtime_node_id,
        name="pool-two",
        model_provider="claude",
        assistant_pool=True,
    )
    db_session.add(second)
    await db_session.commit()
    agents[second.id] = agents[old.id]
    assert (await _switch(client, bot, second)).status_code == 200
    assert await db_session.scalar(select(AssistantRetiredDir)) is None


async def test_ordinary_switches_retire_nothing(client, db_session, monkeypatch, tmp_path):
    bot, old, target, _, _ = await _real_agents(db_session, tmp_path, monkeypatch)
    creator = await db_session.get(User, bot.created_by)
    await login_existing(client, db_session, creator)
    response = await _switch(client, bot, target)
    assert response.status_code == 200, response.text
    assert await db_session.scalar(select(AssistantRetiredDir)) is None
    assert (tmp_path / "source" / bot.bot_key).exists()
