"""Partner intent is global; membership and transport proof belong to the current request."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from coreman.core.bus import tasks
from coreman.core.chat import bot_collaboration as service
from coreman.core.chat import collaboration_setup as setup_service
from coreman.core.chat.collaboration_tools import invoke
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    BotAllowedUser,
    BotCollaboration,
    BotCollaborationRoute,
    OutboxItem,
)
from tests.integration.test_collaboration_tools import fresh


async def invoke_request(session, task, actor, key):
    value = await invoke(
        session,
        task_id=task.id,
        actor=str(actor.id),
        name="request_collaboration",
        arguments={"collaborator_id": key, "question": "核实当前库存"},
        cipher=Cipher(b"t" * 32),
    )
    await session.commit()
    return value


async def test_discovery_does_not_require_group_route(db_session):
    a, b, actor, route, task = await fresh(db_session)
    route.chat_id = "another-group"
    await db_session.commit()
    result = await invoke(
        db_session, task_id=task.id, actor=str(actor.id), name="search_collaborators", arguments={}
    )
    assert [row["id"] for row in result["items"]] == [b.bot_key]


@pytest.mark.parametrize("reason", ["「B」不在当前群", "暂时无法确认「B」是否在当前群"])
async def test_failed_membership_is_durable_and_never_retried(db_session, monkeypatch, reason):
    a, b, actor, route, task = await fresh(db_session)
    check = AsyncMock(side_effect=ValueError(reason))
    monkeypatch.setattr(setup_service, "check_current_group", check)
    first = await invoke_request(db_session, task, actor, b.bot_key)
    second = await invoke_request(db_session, task, actor, "different-peer")
    assert first["stop"] is True and second["stop"] is True
    assert first["error"] == second["error"] == reason
    assert check.await_count == 1
    assert not await db_session.scalar(
        select(OutboxItem.id).where(OutboxItem.payload.has_key("_collaboration_setup_id"))
    )
    assert not await db_session.scalar(
        select(BotCollaboration).where(BotCollaboration.source_task_id == task.id)
    )
    await db_session.refresh(task)
    assert task.payload["collaboration_attempt_error"] == reason
    stopped = await invoke_request(db_session, task, actor, "third-distinct-peer")
    assert stopped["error"] == "collaboration_budget_exhausted"
    await db_session.refresh(task)
    assert task.payload["collaboration_budget"]["failures"] == 3
    assert check.await_count == 1


async def test_target_acl_blocks_before_membership_request(db_session, monkeypatch):
    a, b, actor, route, task = await fresh(db_session)
    db_session.add(BotAllowedUser(bot_id=b.id, user_id=a.created_by))
    await db_session.commit()
    check = AsyncMock()
    monkeypatch.setattr(setup_service, "check_current_group", check)
    result = await invoke_request(db_session, task, actor, b.bot_key)
    assert result["stop"] and "cannot use peer" in result["error"]
    check.assert_not_awaited()


async def test_lazy_identity_probe_is_single_and_no_ai_request_until_verified(
    db_session, monkeypatch
):
    a, b, actor, old_route, task = await fresh(db_session)
    old_route.chat_id = "old-group"
    await db_session.commit()
    check = AsyncMock()
    monkeypatch.setattr(setup_service, "check_current_group", check)
    monkeypatch.setattr(setup_service, "available", AsyncMock(return_value=True))
    monkeypatch.setattr(
        setup_service, "credentials", lambda bot, _: {"app_id": str(bot.id), "app_secret": "s"}
    )
    monkeypatch.setattr(setup_service, "_open_id", AsyncMock(side_effect=["oa", "ob"]))
    result = await invoke_request(db_session, task, actor, b.bot_key)
    assert result["status"] == "waiting_identity" and result["stop"]
    row = await db_session.scalar(
        select(BotCollaboration).where(BotCollaboration.source_task_id == task.id)
    )
    route = await db_session.get(BotCollaborationRoute, row.route_id)
    assert route.chat_id == "group" and route.setup["runtime_request"]
    items = list(
        await db_session.scalars(
            select(OutboxItem).where(
                OutboxItem.payload["_collaboration_setup_id"].astext == str(route.id)
            )
        )
    )
    assert len(items) == 2 and row.request_outbox_id is None
    again = await invoke_request(db_session, task, actor, b.bot_key)
    assert again["collaboration_id"] == result["collaboration_id"]
    assert check.await_count == 1
    await tasks.finish(db_session, task.id, status="succeeded")
    await db_session.commit()
    await service.tick(db_session, datetime.now(UTC))
    assert row.status == "waiting_identity" and row.request_outbox_id is None
    # Verified transport receipt proof is the only transition that unlocks the question.
    route.setup = {**route.setup, "status": "ready"}
    route.tenant_key, route.source_union_id, route.target_union_id = "tenant", "ua", "ub"
    await db_session.commit()
    await service.tick(db_session, datetime.now(UTC))
    assert row.status == "waiting_helper" and row.request_outbox_id is not None


async def test_ready_transport_proof_is_not_bound_to_first_requester(db_session, monkeypatch):
    from coreman.core.db.models import BotCollaborationPartner, User

    a, b, actor, route, task = await fresh(db_session)
    previous_human = await db_session.get(User, a.created_by)
    previous_human.status = "disabled"
    partner = await db_session.scalar(
        select(BotCollaborationPartner).where(BotCollaborationPartner.source_bot_id == a.id)
    )
    route.setup = {
        "runtime_request": True,
        "status": "ready",
        "partner_id": str(partner.id),
        "actor_id": str(previous_human.id),
        "source_fingerprint": setup_service.fingerprint(a),
        "target_fingerprint": setup_service.fingerprint(b),
    }
    db_session.add(BotAllowedUser(bot_id=b.id, user_id=actor.id))
    await db_session.commit()
    monkeypatch.setattr(setup_service, "available", AsyncMock(return_value=True))
    assert (await service.authorized(db_session, route, "human-id", actor.id)).user_id == actor.id
    # Pending notifications still require their initiating human's permission.
    route.setup = {**route.setup, "status": "pending"}
    await db_session.commit()
    with pytest.raises(ValueError):
        await setup_service.guard_probe(
            db_session, OutboxItem(payload={"_collaboration_setup_id": str(route.id)})
        )


async def test_third_identical_failed_request_hits_repeat_budget_without_network(
    db_session, monkeypatch
):
    a, b, actor, route, task = await fresh(db_session)
    check = AsyncMock(side_effect=ValueError("暂时无法确认伙伴是否在当前群"))
    monkeypatch.setattr(setup_service, "check_current_group", check)
    first = await invoke_request(db_session, task, actor, b.bot_key)
    second = await invoke_request(db_session, task, actor, b.bot_key)
    third = await invoke_request(db_session, task, actor, b.bot_key)
    assert first["error"] == second["error"]
    assert first["stop"] and second["stop"] and third["stop"]
    assert third["error"] == "collaboration_budget_exhausted"
    assert check.await_count == 1
    await db_session.refresh(task)
    assert task.cancel_reason == "collaboration_budget_exhausted"
