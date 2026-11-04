import time
from datetime import UTC, datetime

import httpx

from coreman.core.auth.signatures import sign_request
from tests.api.conftest import login_as
from tests.fakes.runtime_node import add_node, add_node_relay


async def test_browser_scope_opt_in_and_no_store(
    client: httpx.AsyncClient, db_session, monkeypatch
):
    await login_as(client, db_session, role="platform_admin")
    credentials = {}
    for scope in ("relay", "runtime-browser"):
        credentials[scope] = (
            await client.post(
                "/api/admin/api-clients",
                json={"app_key": scope + "-reader", "name": scope, "scopes": [scope]},
            )
        ).json()["data"]["secret"]
    node = await add_node(db_session, capabilities={"claude": {"browser_monitor_v1": True}})
    relay = await add_node_relay(db_session, node=node)
    await db_session.commit()
    path = f"/api/infra/relay/servers/{relay.id}/browser-snapshot"

    def headers(scope):
        ts = str(int(time.time()))
        return {
            "X-App-Key": scope + "-reader",
            "X-Timestamp": ts,
            "X-Signature": sign_request("GET", path, {}, ts, scope + "-reader", credentials[scope]),
        }

    assert (await client.get(path, headers=headers("relay"))).status_code == 403
    calls = []

    async def snapshot(*args):
        calls.append(args)
        return {
            "success": True,
            "state": "ok",
            "observed_at": datetime.now(UTC).isoformat(),
            "items": [],
        }

    monkeypatch.setattr("coreman.api.routers.infra_relay.call_agent", snapshot)
    response = await client.get(path, headers=headers("runtime-browser"))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert calls[0][2:] == ("browser-snapshot", {"capture": False, "event": False})
    node.capabilities = {"claude": {"browser_monitor_v1": False}}
    await db_session.commit()
    assert (await client.get(path, headers=headers("runtime-browser"))).status_code == 409
