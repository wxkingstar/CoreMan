"""Admin collaboration boundaries and non-destructive lifecycle."""

import uuid

import pytest

from coreman.core.db.models import BotCollaboration, User
from tests.api.conftest import login_as, login_existing
from tests.integration.test_bot_collaboration import setup


@pytest.fixture
async def configured(client, db_session):
    a, b, human, route, task, ledger = await setup(db_session)
    owner = await db_session.get(User, a.created_by)
    await login_existing(client, db_session, owner)
    return a, b, route, ledger


def endpoint(a, suffix=""):
    return f"/api/admin/bots/{a.id}/collaborators{suffix}"


async def test_only_source_manager_can_read_or_mutate(client, db_session, configured):
    a, b, route, _ = configured
    await login_as(client, db_session, role="platform_admin")
    assert (await client.get(endpoint(a))).status_code == 403
    assert (await client.get(f"/api/admin/bots/{a.id}/collaborator-options")).status_code == 403
    assert (
        await client.patch(
            endpoint(a, f"/{route.id}"), json={"enabled": False}, headers={"If-Match": '"1"'}
        )
    ).status_code == 403


async def test_pause_and_archive_keep_history(client, db_session, configured):
    a, b, route, ledger = configured
    response = await client.patch(
        endpoint(a, f"/{route.id}"), json={"enabled": False}, headers={"If-Match": '"1"'}
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["enabled"] is False
    assert body["version"] == 2
    assert body["active_count"] == 0
    response = await client.delete(endpoint(a, f"/{route.id}"), headers={"If-Match": '"2"'})
    assert response.status_code == 204, response.text
    await db_session.refresh(route)
    await db_session.refresh(ledger)
    assert route.archived and not route.enabled
    assert ledger.status not in {
        "requested",
        "waiting_helper",
        "helper_running",
        "waiting_source",
        "resuming",
    }
    assert await db_session.get(BotCollaboration, ledger.id) is not None
    assert (await client.get(endpoint(a))).json()["data"] == []


async def test_version_and_cross_source_guards(client, db_session, configured):
    a, b, route, _ = configured
    assert (
        await client.patch(endpoint(a, f"/{route.id}"), json={"enabled": False})
    ).status_code == 428
    assert (
        await client.patch(
            endpoint(a, f"/{route.id}"), json={"enabled": False}, headers={"If-Match": '"99"'}
        )
    ).status_code == 409
    assert (
        await client.delete(endpoint(b, f"/{route.id}"), headers={"If-Match": '"1"'})
    ).status_code == 404


async def test_self_and_unmanaged_target_rejected_before_platform_calls(
    client, db_session, configured
):
    a, b, route, _ = configured
    response = await client.post(endpoint(a), json={"target_bot_id": str(a.id), "chat_id": "group"})
    assert response.status_code == 422
    stranger = User(login_name="other-owner", display_name="Other")
    db_session.add(stranger)
    await db_session.flush()
    b.created_by = stranger.id
    await db_session.commit()
    response = await client.post(endpoint(a), json={"target_bot_id": str(b.id), "chat_id": "group"})
    assert response.status_code == 403
    options = await client.get(f"/api/admin/bots/{a.id}/collaborator-options")
    assert options.status_code == 200
    assert options.json()["data"] == []
    # Losing target management must never prevent stopping the source's existing route.
    response = await client.patch(
        endpoint(a, f"/{route.id}"), json={"enabled": False}, headers={"If-Match": '"1"'}
    )
    assert response.status_code == 200


async def test_csrf_required_and_unknown_route_not_found(client, configured):
    a, _, _, _ = configured
    csrf = client.headers.pop("X-CSRF-Token")
    assert (
        await client.post(
            endpoint(a), json={"target_bot_id": str(uuid.uuid4()), "chat_id": "group"}
        )
    ).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert (
        await client.delete(endpoint(a, f"/{uuid.uuid4()}"), headers={"If-Match": '"1"'})
    ).status_code == 404


async def test_create_verify_enable_flow_has_envelope_and_version(client, db_session, monkeypatch):
    from coreman.core.db.models import OutboxItem
    from tests.integration.test_collaboration_setup import prepared, receipt

    a, b, route, owner, _ = await prepared(db_session, monkeypatch)
    await login_existing(client, db_session, owner)
    response = await client.get(
        f"/api/admin/bots/{a.id}/collaborator-groups", params={"target_bot_id": str(b.id)}
    )
    assert response.json() == {"code": 0, "data": [{"chat_id": "group", "name": "测试群"}]}
    response = await client.get(endpoint(a))
    initial = response.json()["data"][0]
    assert initial["status"] == "pending" and not initial["can_enable"]
    rejected = await client.patch(
        endpoint(a, f"/{route.id}"),
        json={"enabled": True},
        headers={"If-Match": f'"{initial["version"]}"'},
    )
    assert rejected.status_code == 422
    duplicate = await client.post(
        endpoint(a), json={"target_bot_id": str(b.id), "chat_id": "group"}
    )
    assert duplicate.status_code == 409
    source = await db_session.get(OutboxItem, route.setup["source_outbox_id"])
    target = await db_session.get(OutboxItem, route.setup["target_outbox_id"])
    await receipt(db_session, b, source, "app-b", "open-app-b", "union-a")
    await receipt(db_session, a, target, "app-a", "open-app-a", "union-b")
    await db_session.commit()
    response = await client.get(endpoint(a))
    ready = response.json()["data"][0]
    assert ready["status"] == "ready" and not ready["enabled"] and ready["can_enable"]
    assert ready["version"] > initial["version"]
    response = await client.patch(
        endpoint(a, f"/{route.id}"),
        json={"enabled": True},
        headers={"If-Match": f'"{ready["version"]}"'},
    )
    assert response.status_code == 200, response.text
    enabled = response.json()["data"]
    assert enabled["enabled"] and int(response.headers["etag"].strip('"')) == enabled["version"]
    await db_session.refresh(route)
    assert route.version == enabled["version"]


async def test_archived_connection_can_be_recreated_but_old_probe_cannot_send(
    client, db_session, monkeypatch
):
    from coreman.core.chat import collaboration_setup
    from coreman.core.db.models import OutboxItem
    from tests.integration.test_collaboration_setup import prepared

    a, b, route, owner, _ = await prepared(db_session, monkeypatch)
    await login_existing(client, db_session, owner)
    old = await db_session.get(OutboxItem, route.setup["source_outbox_id"])
    old_probe = route.setup["probe_id"]
    response = await client.delete(
        endpoint(a, f"/{route.id}"), headers={"If-Match": f'"{route.version}"'}
    )
    assert response.status_code == 204
    response = await client.post(endpoint(a), json={"target_bot_id": str(b.id), "chat_id": "group"})
    assert response.status_code == 201, response.text
    result = response.json()["data"]
    assert result["status"] == "pending" and not result["enabled"]
    await db_session.refresh(route)
    assert route.setup["probe_id"] != old_probe and not route.archived
    with pytest.raises(ValueError):
        await collaboration_setup.guard_probe(db_session, old)
