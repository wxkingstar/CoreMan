"""Real DB tests for discovery authorization and durable loop guards."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from coreman.core.bus import tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import BotAllowedUser, BotCollaboration
from tests.integration.test_bot_collaboration import setup
from tests.integration.test_chat_handler import chat_task


async def fresh(session):
    a, b, actor, route, old, row = await setup(session)
    row.status = "completed"
    await tasks.finish(session, old.id, status="succeeded")
    b.description = "日本仓 库存查询 可用数量"
    task = await chat_task(
        session, a, "查询库存", sender="human-id", chat_type="group", chat_id="group"
    )
    await session.commit()
    return a, b, actor, route, task


async def call(session, task, actor, name, args):
    from coreman.core.chat.collaboration_tools import invoke

    if name == "request_collaboration":
        with (
            patch(
                "coreman.core.chat.collaboration_setup.check_current_group", new_callable=AsyncMock
            ),
            patch(
                "coreman.core.chat.collaboration_setup.available", new=AsyncMock(return_value=True)
            ),
            patch("coreman.core.chat.collaboration_setup.begin_runtime", new_callable=AsyncMock),
        ):
            result = await invoke(
                session,
                task_id=task.id,
                actor=str(actor.id),
                name=name,
                arguments=args,
                cipher=Cipher(b"t" * 32),
            )
    else:
        result = await invoke(
            session, task_id=task.id, actor=str(actor.id), name=name, arguments=args
        )
    await session.commit()
    return result


async def test_discover_detail_and_handoff_without_catalog_in_prompt(db_session):
    a, b, actor, route, task = await fresh(db_session)
    result = await call(db_session, task, actor, "search_collaborators", {"query": "库存"})
    assert result["items"][0]["id"] == b.bot_key
    assert "description" not in result["items"][0]
    detail = await call(db_session, task, actor, "get_collaborator", {"id": b.bot_key})
    assert "日本仓" in detail["description"]
    args = {"collaborator_id": b.bot_key, "question": "可用库存?", "context": "只读核对"}
    first = await call(db_session, task, actor, "request_collaboration", args)
    second = await call(db_session, task, actor, "request_collaboration", args)
    assert first["collaboration_id"] == second["collaboration_id"]
    await db_session.refresh(task)
    assert task.payload["collaboration_handoff"] is True
    rows = list(
        await db_session.scalars(
            select(BotCollaboration).where(BotCollaboration.source_task_id == task.id)
        )
    )
    assert len(rows) == 1


async def test_revocation_hides_peer_and_blocks_cached_detail(db_session):
    a, b, actor, route, task = await fresh(db_session)
    assert (await call(db_session, task, actor, "search_collaborators", {}))["items"]
    db_session.add(BotAllowedUser(bot_id=b.id, user_id=a.created_by))
    await db_session.commit()
    assert (await call(db_session, task, actor, "search_collaborators", {}))["items"] == []
    detail = await call(db_session, task, actor, "get_collaborator", {"id": b.bot_key})
    assert detail["error"]


async def test_repeat_search_cancels_instead_of_unbounded_retry(db_session):
    a, b, actor, route, task = await fresh(db_session)
    for _ in range(2):
        assert (await call(db_session, task, actor, "search_collaborators", {}))["items"]
    result = await call(db_session, task, actor, "search_collaborators", {})
    assert result["error"] == "collaboration_budget_exhausted"
    await db_session.refresh(task)
    assert task.cancel_reason == "collaboration_budget_exhausted"
    assert task.cancel_requested_at is not None


async def test_distinct_bad_calls_share_failure_budget(db_session):
    a, b, actor, route, task = await fresh(db_session)
    for n in range(3):
        result = await call(db_session, task, actor, "get_collaborator", {"id": f"missing-{n}"})
    assert result["error"] == "collaboration_budget_exhausted"
    await db_session.refresh(task)
    assert task.cancel_requested_at is not None


@pytest.mark.parametrize("phase", ["helper", "resume"])
async def test_no_discovery_or_delegation_from_descendant(db_session, phase):
    a, b, actor, route, task = await fresh(db_session)
    task.payload = {"collaboration_id": "present", "collaboration_phase": phase}
    await db_session.commit()
    with pytest.raises(ValueError, match="cannot delegate"):
        await call(db_session, task, actor, "search_collaborators", {})


async def test_parallel_calls_cannot_reset_or_overspend_budget(db_session, db_engine):
    a, b, actor, route, task = await fresh(db_session)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def run(n):
        async with factory() as session:
            try:
                return await call(session, task, actor, "search_collaborators", {"query": str(n)})
            except ValueError:
                return {}

    results = await asyncio.gather(*(run(n) for n in range(15)))
    assert sum("items" in result for result in results) == 12
    await db_session.refresh(task)
    assert task.cancel_requested_at is not None
    assert task.payload["collaboration_budget"]["calls"] == 13


async def test_worker_stops_model_after_registered_handoff(db_session, db_engine):
    from tests.fakes.fake_relay import FakeRelay
    from tests.integration.test_chat_handler import run

    a, b, actor, route, task = await fresh(db_session)
    fake = FakeRelay("normal", first_byte_delay=4)
    running = asyncio.create_task(run(db_engine, task, fake))
    try:
        for _ in range(100):
            if fake.requests:
                break
            await asyncio.sleep(0.02)
        assert fake.requests
        await call(
            db_session,
            task,
            actor,
            "request_collaboration",
            {"collaborator_id": b.bot_key, "question": "库存?"},
        )
        await asyncio.wait_for(running, 3)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert fake.aborted == 1
    row = await db_session.scalar(
        select(BotCollaboration).where(BotCollaboration.source_task_id == task.id)
    )
    assert row.status == "waiting_helper"
    await db_session.refresh(task)
    assert task.status == "succeeded"


async def test_worker_tool_loop_is_cut_off(db_session, db_engine, monkeypatch):
    from tests.fakes.fake_relay import DONE, FINISH, SCENARIOS, FakeRelay, _tool
    from tests.integration.test_chat_handler import run

    a, b, actor, route, task = await fresh(db_session)
    monkeypatch.setitem(
        SCENARIOS, "loop", lambda: [_tool("Bash", f"t{n}") for n in range(100)] + [FINISH, DONE]
    )
    fake = FakeRelay("loop")
    await run(db_engine, task, fake)
    await db_session.refresh(task)
    assert task.status == "cancelled"
    assert task.error_code == "collaboration_budget_exhausted"
    assert fake.aborted == 1


async def test_search_pages_and_detail_size_are_bounded(db_session):
    from coreman.core.db.models import Bot, BotCollaborationPartner, BotCollaborationRoute

    a, b, actor, route, task = await fresh(db_session)
    b.description = "库存" * 3000
    for n in range(11):
        peer = Bot(
            bot_key=f"peer-{n:02}",
            platform="feishu",
            name=f"P{n}",
            created_by=a.created_by,
            relay_server_id=a.relay_server_id,
            model=a.model,
            working_dir="/helper",
            description="库存",
            credentials_enc=a.credentials_enc,
            env_vars_enc=a.env_vars_enc,
        )
        db_session.add(peer)
        await db_session.flush()
        db_session.add(BotCollaborationPartner(source_bot_id=a.id, target_bot_id=peer.id))
        db_session.add(
            BotCollaborationRoute(
                source_bot_id=a.id,
                target_bot_id=peer.id,
                chat_id="group",
                tenant_key="tenant",
                source_open_id="oa",
                target_open_id=f"ob{n}",
                source_union_id="ua",
                target_union_id=f"ub{n}",
                enabled=True,
            )
        )
    await db_session.commit()
    page = await call(db_session, task, actor, "search_collaborators", {"query": "库存"})
    assert len(page["items"]) == 5
    assert all(len(item["summary"]) <= 160 for item in page["items"])
    second = await call(
        db_session,
        task,
        actor,
        "search_collaborators",
        {"query": "库存", "cursor": page["next_cursor"]},
    )
    assert len(second["items"]) == 5
    assert not ({item["id"] for item in page["items"]} & {item["id"] for item in second["items"]})
    detail = await call(db_session, task, actor, "get_collaborator", {"id": b.bot_key})
    assert len(detail["description"]) == 4000


async def test_cancellation_at_finalization_cannot_become_success(
    db_session, db_engine, monkeypatch
):
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes.fake_relay import FakeRelay
    from tests.integration.test_chat_handler import run

    a, b, actor, route, task = await fresh(db_session)
    original = ChatTaskHandler._converse

    async def cancel_on_completion(self, ctx, pre):
        outcome = await original(self, ctx, pre)
        async with ctx.session_factory() as session:
            await tasks.request_cancel(session, task.id, "collaboration_budget_exhausted")
            await session.commit()
        return outcome

    monkeypatch.setattr(ChatTaskHandler, "_converse", cancel_on_completion)
    await run(db_engine, task, FakeRelay("normal"))
    await db_session.refresh(task)
    assert task.status == "cancelled"
    assert task.error_code == "collaboration_budget_exhausted"


async def test_helper_loop_stops_without_resuming_source(db_session, db_engine, monkeypatch):
    from datetime import UTC, datetime

    from coreman.core.chat import bot_collaboration as service
    from coreman.core.db.models import OutboxItem
    from tests.fakes.fake_relay import DONE, FINISH, SCENARIOS, FakeRelay, _tool
    from tests.integration.test_bot_collaboration import receipt
    from tests.integration.test_chat_handler import run

    a, b, actor, route, source, row = await setup(db_session)
    await tasks.finish(db_session, source.id, status="succeeded")
    await service.send_message(db_session, row, route)
    item = await db_session.get(OutboxItem, row.request_outbox_id)
    item.payload = {**item.payload, "_feishu_message_id": "request"}
    item.status = "sent"
    await receipt(db_session, b, mid="request", union="ua")
    await db_session.flush()
    await service.tick(db_session, datetime.now(UTC))
    await db_session.commit()
    helper = await tasks.claim(db_session, lane="normal", instance_id="worker-test")
    await db_session.commit()
    assert helper.id == row.helper_task_id
    monkeypatch.setitem(
        SCENARIOS,
        "helper-loop",
        lambda: [_tool("mcp__database__query", f"t{n}") for n in range(100)] + [FINISH, DONE],
    )
    fake = FakeRelay("helper-loop")
    await run(db_engine, helper, fake)
    await db_session.refresh(row)
    assert row.status == "failed"
    assert row.resume_task_id is None
    assert fake.aborted == 1
    await db_session.refresh(helper)
    assert helper.status == "cancelled"


async def test_registered_handoff_survives_eof_before_poll(db_session, db_engine, monkeypatch):
    from coreman.runtime.worker.chat.models import Outcome
    from coreman.runtime.worker.chat_handler import ChatTaskHandler
    from tests.fakes.fake_relay import FakeRelay
    from tests.integration.test_chat_handler import run

    a, b, actor, route, task = await fresh(db_session)

    async def disconnect_after_registration(self, ctx, pre):
        async with ctx.session_factory() as session:
            await call(
                session,
                task,
                actor,
                "request_collaboration",
                {"collaborator_id": b.bot_key, "question": "库存?"},
            )
        return Outcome(error=ConnectionError("EOF before handoff polling"))

    monkeypatch.setattr(ChatTaskHandler, "_converse", disconnect_after_registration)
    await run(db_engine, task, FakeRelay("normal"))
    row = await db_session.scalar(
        select(BotCollaboration).where(BotCollaboration.source_task_id == task.id)
    )
    assert row.status == "waiting_helper"
    await db_session.refresh(task)
    assert task.status == "succeeded"
