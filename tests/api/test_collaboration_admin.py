"""Partner selection is independent of target ownership, group and runtime state."""

import uuid

import pytest
from sqlalchemy import func, select

from coreman.core.db.models import BotCollaboration, BotCollaborationPartner, OutboxItem, User
from tests.api.conftest import login_as, login_existing
from tests.integration.test_bot_collaboration import setup


@pytest.fixture
async def configured(client, db_session):
    a, b, human, route, task, ledger = await setup(db_session)
    partner = await db_session.scalar(
        select(BotCollaborationPartner).where(
            BotCollaborationPartner.source_bot_id == a.id,
            BotCollaborationPartner.target_bot_id == b.id,
        )
    )
    if partner is None:
        partner = BotCollaborationPartner(
            source_bot_id=a.id, target_bot_id=b.id, enabled=True, archived=False, version=1
        )
        db_session.add(partner)
        await db_session.commit()
    owner = await db_session.get(User, a.created_by)
    await login_existing(client, db_session, owner)
    return a, b, partner, ledger, route


def endpoint(a, suffix=""):
    return f"/api/admin/bots/{a.id}/collaborators{suffix}"


async def test_only_source_manager_can_read_or_mutate(client, db_session, configured):
    a, _, partner, _, _ = configured
    await login_as(client, db_session, role="platform_admin")
    assert (await client.get(endpoint(a))).status_code == 403
    assert (await client.get(f"/api/admin/bots/{a.id}/collaborator-options")).status_code == 403
    assert (
        await client.patch(
            endpoint(a, f"/{partner.id}"), json={"enabled": False}, headers={"If-Match": '"1"'}
        )
    ).status_code == 403


async def test_pause_archive_and_recreate_keep_history(client, db_session, configured):
    a, b, partner, ledger, route = configured
    response = await client.patch(
        endpoint(a, f"/{partner.id}"), json={"enabled": False}, headers={"If-Match": '"1"'}
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert not body["enabled"] and body["version"] == 2 and body["active_count"] == 0
    response = await client.delete(endpoint(a, f"/{partner.id}"), headers={"If-Match": '"2"'})
    assert response.status_code == 204, response.text
    await db_session.refresh(partner)
    await db_session.refresh(route)
    await db_session.refresh(ledger)
    assert partner.archived and not partner.enabled and not route.enabled
    assert ledger.status == "cancelled"
    assert await db_session.get(BotCollaboration, ledger.id) is not None
    assert (await client.get(endpoint(a))).json()["data"] == []
    response = await client.post(endpoint(a), json={"target_bot_id": str(b.id)})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["id"] == str(partner.id)
    assert response.json()["data"]["enabled"]
    await db_session.refresh(ledger)
    assert ledger.status == "cancelled"


async def test_version_and_cross_source_guards(client, configured):
    a, b, partner, _, _ = configured
    assert (
        await client.patch(endpoint(a, f"/{partner.id}"), json={"enabled": False})
    ).status_code == 428
    assert (
        await client.patch(
            endpoint(a, f"/{partner.id}"), json={"enabled": False}, headers={"If-Match": '"99"'}
        )
    ).status_code == 409
    assert (
        await client.delete(endpoint(b, f"/{partner.id}"), headers={"If-Match": '"1"'})
    ).status_code == 404


async def test_other_owner_and_offline_partner_selectable_without_platform_calls(
    client, db_session, configured, monkeypatch
):
    from coreman.core.platforms.feishu import FeishuClient

    async def forbidden_call(*args, **kwargs):
        pytest.fail("Partner configuration must not call Feishu")

    monkeypatch.setattr(FeishuClient, "call", forbidden_call)
    a, b, partner, _, _ = configured
    stranger = User(login_name="other-owner", display_name="Other")
    db_session.add(stranger)
    await db_session.flush()
    b.created_by, b.enabled = stranger.id, False
    partner.archived = True
    await db_session.commit()
    before = await db_session.scalar(select(func.count()).select_from(OutboxItem))
    response = await client.get(f"/api/admin/bots/{a.id}/collaborator-options")
    assert response.status_code == 200
    assert [r["id"] for r in response.json()["data"]] == [str(b.id)]
    assert not response.json()["data"][0]["available"]
    response = await client.post(endpoint(a), json={"target_bot_id": str(b.id)})
    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["enabled"] and body["status"] == "unavailable" and "chat_id" not in body
    assert int(response.headers["etag"].strip('"')) == body["version"]
    assert await db_session.scalar(select(func.count()).select_from(OutboxItem)) == before
    assert (await client.post(endpoint(a), json={"target_bot_id": str(b.id)})).status_code == 409


async def test_partner_readiness_requires_verified_transport_and_exposes_known_failure(
    client, db_session, configured, monkeypatch
):
    from unittest.mock import AsyncMock

    from coreman.core.chat import collaboration_setup

    a, b, partner, _, route = configured
    monkeypatch.setattr(collaboration_setup, "available", AsyncMock(return_value=True))
    route.enabled = True
    route.archived = False
    route.setup = {"status": "failed", "reason": "probe_delivery_failed"}
    await db_session.commit()

    body = (await client.get(endpoint(a))).json()["data"][0]
    assert body["status"] == "runtime_ready"
    assert body["configured"] is True
    assert body["runtime_ready"] is True
    assert body["transport_verified"] is False
    assert body["verification_error"] == "probe_delivery_failed"

    route.setup = {
        "status": "ready",
        "reason": None,
        "runtime_request": True,
        "partner_id": str(partner.id),
        "source_fingerprint": collaboration_setup.fingerprint(a),
        "target_fingerprint": collaboration_setup.fingerprint(b),
    }
    await db_session.commit()
    body = (await client.get(endpoint(a))).json()["data"][0]
    assert body["status"] == "verified"
    assert body["transport_verified"] is True
    assert body["verification_error"] is None

    partner.enabled = False
    await db_session.commit()
    body = (await client.get(endpoint(a))).json()["data"][0]
    assert body["status"] == "runtime_ready"
    assert body["transport_verified"] is False
    assert body["verification_error"] == "permission_revoked"


async def test_self_wrong_platform_and_stale_group_body_rejected(client, db_session, configured):
    a, b, _, _, _ = configured
    assert (await client.post(endpoint(a), json={"target_bot_id": str(a.id)})).status_code == 422
    assert (
        await client.post(endpoint(a), json={"target_bot_id": str(b.id), "chat_id": "old"})
    ).status_code == 422
    b.platform = "wecom"
    await db_session.commit()
    assert (await client.post(endpoint(a), json={"target_bot_id": str(b.id)})).status_code == 422
    assert (await client.get(f"/api/admin/bots/{a.id}/collaborator-options")).json()["data"] == []


async def test_csrf_required_and_unknown_partner_not_found(client, configured):
    a, _, _, _, _ = configured
    csrf = client.headers.pop("X-CSRF-Token")
    assert (
        await client.post(endpoint(a), json={"target_bot_id": str(uuid.uuid4())})
    ).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert (
        await client.delete(endpoint(a, f"/{uuid.uuid4()}"), headers={"If-Match": '"1"'})
    ).status_code == 404


async def test_pause_waiting_ledger_does_not_lock_transport_first(
    db_session, db_engine, configured
):
    """A scheduler holding a ledger must still be able to reconcile its route."""
    import asyncio

    from sqlalchemy import text

    from coreman.api.routers.bot_collaboration_admin import stop
    from coreman.core.db.models import BotCollaborationRoute
    from coreman.core.db.session import make_session_factory

    _, _, partner, ledger, route = configured
    await db_session.commit()
    factory = make_session_factory(db_engine)
    async with factory() as scheduler, factory() as admin:
        await scheduler.get(BotCollaboration, ledger.id, with_for_update=True)
        pid = await admin.scalar(text("select pg_backend_pid()"))
        candidate = await admin.get(BotCollaborationPartner, partner.id)
        operation = asyncio.create_task(stop(admin, candidate))
        try:
            async with asyncio.timeout(3):
                while not await scheduler.scalar(  # noqa: ASYNC110 - observes PostgreSQL lock state
                    text("select exists(select 1 from pg_locks where pid=:pid and not granted)"),
                    {"pid": pid},
                ):
                    await asyncio.sleep(0.01)
            # NOWAIT fails immediately if admin took the route before waiting on our ledger.
            await scheduler.scalar(
                select(BotCollaborationRoute)
                .where(BotCollaborationRoute.id == route.id)
                .with_for_update(nowait=True)
            )
            await scheduler.commit()
            await asyncio.wait_for(operation, 3)
            await admin.commit()
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)


async def test_pause_covers_all_pair_groups_and_invalidates_pending_probe(
    client, db_session, configured
):
    from coreman.core.chat import collaboration_setup
    from coreman.core.db.models import BotCollaborationRoute
    from tests.integration.test_chat_handler import chat_task

    a, b, partner, ledger, route = configured
    other = BotCollaborationRoute(
        source_bot_id=a.id,
        target_bot_id=b.id,
        chat_id="group-two",
        tenant_key="tenant",
        source_open_id="oa",
        target_open_id="ob",
        source_union_id="ua",
        target_union_id="ub",
        enabled=True,
        setup={"status": "pending", "probe_id": "old-probe"},
    )
    unrelated = BotCollaborationRoute(
        source_bot_id=b.id,
        target_bot_id=a.id,
        chat_id="group",
        tenant_key="tenant",
        source_open_id="ob",
        target_open_id="oa",
        source_union_id="ub",
        target_union_id="ua",
        enabled=True,
        setup={},
    )
    db_session.add_all([other, unrelated])
    await db_session.flush()
    from coreman.core.bus import outbox

    probe = await outbox.add(
        db_session,
        bot_id=a.id,
        platform="feishu",
        kind="send",
        dedupe_key="pending-partner-proof",
        target={"chat_id": "group-two"},
        payload={"text": "probe"},
    )
    other.setup = {**other.setup, "source_outbox_id": probe.id}

    task = await chat_task(
        db_session, a, "second", sender="human-id", chat_type="group", chat_id="group-two"
    )
    row = BotCollaboration(
        route_id=other.id,
        source_task_id=task.id,
        origin_user_id=ledger.origin_user_id,
        origin_platform_user_id=ledger.origin_platform_user_id,
        source_session_key="group-two",
        source_relay_session_id=ledger.source_relay_session_id,
        source_relay_id=ledger.source_relay_id,
        question="second",
        status="waiting_identity",
        expires_at=ledger.expires_at,
    )
    db_session.add(row)
    await db_session.commit()
    response = await client.patch(
        endpoint(a, f"/{partner.id}"), json={"enabled": False}, headers={"If-Match": '"1"'}
    )
    assert response.status_code == 200, response.text
    for obj in (row, other, unrelated):
        await db_session.refresh(obj)
    assert row.status == "cancelled" and not other.enabled
    assert other.setup["status"] == "failed" and unrelated.enabled
    await db_session.refresh(probe)
    assert probe.status == "skipped"
    # Re-enabling configuration cannot reactivate the old pending transport proof.
    response = await client.patch(
        endpoint(a, f"/{partner.id}"), json={"enabled": True}, headers={"If-Match": '"2"'}
    )
    assert response.status_code == 200, response.text
    with pytest.raises(ValueError, match="verification_cancelled"):
        await collaboration_setup._current(db_session, other)
