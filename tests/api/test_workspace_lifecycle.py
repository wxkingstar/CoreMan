"""Lifecycle integration: initialization, edits and the task-claim fence share real DB state."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from coreman.core.bots import workspace_transfer
from coreman.core.bus import tasks
from coreman.core.db.models import Bot, ProcessInstance, Task
from coreman.core.db.session import make_session_factory
from coreman.core.timeutils import utcnow
from tests.api.conftest import login_as
from tests.api.test_bots import _bot_body, _team
from tests.fakes.runtime_node import add_node_relay


@pytest.fixture
async def process_instances(db_session):
    now = utcnow()
    worker = ProcessInstance(
        id="worker:workspace-lifecycle",
        service="worker",
        version="test",
        started_at=now,
        heartbeat_at=now,
        capacity=2,
    )
    db_session.add(worker)
    await db_session.commit()
    return worker


async def assigned_creation(client, db_session):
    team = await _team(db_session)
    await login_as(client, db_session, team_id=team.id)
    relay = await add_node_relay(db_session)
    await db_session.commit()
    return _bot_body(relay_server_id=str(relay.id))


async def test_assigned_creation_initializes_instructions_before_ready(
    client,
    db_session,
    monkeypatch,
):
    body = await assigned_creation(client, db_session)
    seen = []
    original_content = workspace_transfer.instruction_content

    def inspect_content(bot):
        assert bot.workspace_state == "initializing"
        content = original_content(bot)
        seen.append(content)
        return content

    async def runtime(relay, cipher, operation, payload=None):
        if operation == "ping":
            return {"success": True, "workspace_protocol": 1}
        assert operation == "workspace-init"
        assert payload["working_dir"] == body["working_dir"]
        assert payload["bot_id"]
        assert payload["content"] == seen[0]
        assert "销售助手" in payload["content"] and "卖货" in payload["content"]
        assert "记忆" in payload["content"]
        assert "wecom-secret-value" not in payload["content"]
        assert "pw-123456" not in payload["content"]
        return {"success": True, "initialized": True}

    monkeypatch.setattr(workspace_transfer, "instruction_content", inspect_content)
    agent = AsyncMock(side_effect=runtime)
    monkeypatch.setattr(workspace_transfer, "call_agent", agent)
    response = await client.post("/api/admin/bots", json=body)
    assert response.status_code == 201, response.text
    bot = await db_session.scalar(select(Bot))
    assert bot.workspace_state == "ready"
    assert bot.workspace_error is None
    assert [call.args[2] for call in agent.call_args_list] == ["ping", "workspace-init"]
    assert len(seen) == 1


async def test_old_daemon_creation_is_failed_and_cannot_execute(
    client,
    db_session,
    db_engine,
    monkeypatch,
    process_instances,
):
    body = await assigned_creation(client, db_session)
    agent = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(workspace_transfer, "call_agent", agent)
    response = await client.post("/api/admin/bots", json=body)
    assert response.status_code == 201, response.text
    bot = await db_session.scalar(select(Bot))
    assert bot.workspace_state == "failed"
    assert "升级" in bot.workspace_error
    agent.assert_awaited_once()
    task = Task(bot_id=bot.id, kind="chat", payload={})
    db_session.add(task)
    await db_session.commit()
    factory = make_session_factory(db_engine)
    async with factory() as worker:
        assert await tasks.claim(worker, lane="normal", instance_id=process_instances.id) is None
    await db_session.refresh(task)
    assert task.status == "queued" and task.attempts == 0


async def seeded_queue(client, db_session, *, state="ready", lane="normal", kind="chat"):
    creator = await login_as(client, db_session)
    bot = Bot(
        bot_key="lifecycle-test",
        name="员工",
        platform="wecom",
        created_by=creator.id,
        model="claude-sonnet-4-6",
        working_dir="/home/ai/lifecycle-test",
        credentials_enc="unused",
        workspace_state=state,
    )
    db_session.add(bot)
    await db_session.flush()
    queued = Task(bot_id=bot.id, kind=kind, lane=lane, payload={})
    db_session.add(queued)
    await db_session.commit()
    return bot, queued


async def test_claim_skips_bot_row_lock_then_resumes(
    client,
    db_session,
    db_engine,
    process_instances,
):
    bot, queued = await seeded_queue(client, db_session)
    factory = make_session_factory(db_engine)
    async with factory() as operation, factory() as worker:
        await operation.scalar(select(Bot).where(Bot.id == bot.id).with_for_update())
        claim = await asyncio.wait_for(
            tasks.claim(worker, lane="normal", instance_id=process_instances.id),
            timeout=2,
        )
        assert claim is None
        await worker.commit()
        await operation.commit()
        claim = await tasks.claim(worker, lane="normal", instance_id=process_instances.id)
        assert claim is not None and claim.id == queued.id
        await worker.commit()
    await db_session.refresh(queued)
    assert queued.status == "claimed" and queued.attempts == 1


@pytest.mark.parametrize("state", ["pending", "initializing", "migrating", "failed", "busy"])
@pytest.mark.parametrize(
    "lane,kind", [("normal", "chat"), ("fast", "command"), ("normal", "cron_run")]
)
async def test_nonready_states_fence_every_lane_and_ready_resumes(
    client,
    db_session,
    db_engine,
    process_instances,
    state,
    lane,
    kind,
):
    bot, queued = await seeded_queue(client, db_session, state=state, lane=lane, kind=kind)
    factory = make_session_factory(db_engine)
    async with factory() as worker:
        assert await tasks.claim(worker, lane=lane, instance_id=process_instances.id) is None
        await worker.commit()
        bot.workspace_state = "ready"
        await db_session.commit()
        claimed = await tasks.claim(worker, lane=lane, instance_id=process_instances.id)
        assert claimed is not None and claimed.id == queued.id
        await worker.commit()
    await db_session.refresh(queued)
    assert queued.status == "claimed" and queued.attempts == 1


async def test_bound_directory_cannot_be_patched_without_migration(client, db_session, monkeypatch):
    body = await assigned_creation(client, db_session)
    monkeypatch.setattr(
        workspace_transfer,
        "call_agent",
        AsyncMock(
            return_value={
                "success": True,
                "workspace_protocol": 1,
                "initialized": True,
            }
        ),
    )
    created = await client.post("/api/admin/bots", json=body)
    assert created.status_code == 201, created.text
    bot = created.json()["data"]
    response = await client.patch(
        f"/api/admin/bots/{bot['id']}",
        json={"working_dir": "/data/skills/new-directory"},
        headers={"If-Match": f'"{bot["version"]}"'},
    )
    assert response.status_code == 409, response.text
    assert "迁移" in response.text
    persisted = await db_session.scalar(select(Bot))
    assert persisted.working_dir == body["working_dir"]
