"""Stateful checks for remaining callback, compatibility and event-stream paths."""

import hashlib
import time
import uuid

import httpx
import pytest

from tests.api.conftest import login_as
from tests.api.test_escalations import setup
from tests.api.test_runtime_nodes import enrollment
from tests.unit.test_callback_crypto import encrypted_message


async def test_wecom_challenge_signature_and_corporation(client, db_session):
    _, _, app, _ = await setup(client, db_session)
    path = f"/api/callbacks/wecom/{app.id}"
    timestamp = str(int(time.time()))

    def params(receiver):
        encrypted, _ = encrypted_message(b"qa-challenge", receiver=receiver)
        signature = hashlib.sha1(
            "".join(sorted(("token", timestamp, "nonce", encrypted))).encode()
        ).hexdigest()
        return dict(echostr=encrypted, msg_signature=signature, timestamp=timestamp, nonce="nonce")

    valid = params("ww-test")
    result = await client.get(path, params=valid)
    assert result.status_code == 200 and result.content == b"qa-challenge"
    assert (await client.get(path, params=valid | {"nonce": "tampered"})).status_code == 403
    assert (await client.get(path, params=params("other-corp"))).status_code == 403
    assert (
        await client.get(f"/api/callbacks/wecom/{uuid.uuid4()}", params=valid)
    ).status_code == 403


async def test_legacy_escalation_resolve_and_cancel_terminal_state(client, db_session):
    bot, _, _, _ = await setup(client, db_session)
    body = {"bot_key": bot.bot_key, "to_user_id": "recipient", "question": "QA"}
    first = (await client.post("/api/escalation/create", json=body)).json()["data"]
    path = f"/api/escalation/{first['group_id']}"
    assert (await client.post(path + "/resolve", json={"resolution": "agent"})).status_code == 409
    response = await client.post(path + "/resolve", json={"resolution": "observed"})
    assert response.status_code == 200 and response.json()["data"]["status"] == "completed"
    assert (await client.get(path + "/poll")).json()["data"]["resolution"] == "observed"
    second = (await client.post("/api/escalation/create", json=body)).json()["data"]
    path = f"/api/escalation/{second['group_id']}"
    assert (await client.post(path + "/cancel")).status_code == 200
    assert (await client.get(path + "/poll")).json()["data"]["status"] == "cancelled"
    assert (await client.post(path + "/followup", json={"question": "revive"})).status_code == 409


@pytest.mark.parametrize("upstream_status", [200, 404])
async def test_runtime_event_stream_forwarding_and_access(
    client, db_session, monkeypatch, upstream_status
):
    _, node, _ = await enrollment(client, db_session)
    payload = b'event: message\ndata: {"text":"qa-event"}\n\n'

    def handle(request):
        assert request.url.path == "/session/qa-session/events"
        return httpx.Response(upstream_status, content=payload)

    monkeypatch.setattr(
        "coreman.core.runtime_nodes.transport.ReverseTransport",
        lambda node_id, provider: httpx.MockTransport(handle),
    )
    path = f"/api/admin/runtime-nodes/{node['node_id']}/claude/session/qa-session/events"
    response = await client.get(path)
    assert response.status_code == 200
    assert response.content == (
        payload if upstream_status == 200 else b"event: unavailable\ndata: {}\n\n"
    )
    assert "no-store" in response.headers["cache-control"]
    await login_as(client, db_session, role="member")
    assert (await client.get(path)).status_code == 403


async def test_user_detail_matches_directory_and_requires_login(client, db_session):
    user = await login_as(client, db_session, role="member", display_name="QA directory")
    path = f"/api/admin/users/{user.id}"
    detail = await client.get(path)
    listing = await client.get("/api/admin/users")
    assert detail.status_code == 200
    assert detail.json()["data"] == listing.json()["data"]["items"][0]
    assert (await client.get(f"/api/admin/users/{uuid.uuid4()}")).status_code == 404
    client.cookies.clear()
    assert (await client.get(path)).status_code == 401
