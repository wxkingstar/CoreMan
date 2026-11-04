import base64
import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.signatures import sign_request
from coreman.core.crypto import Cipher
from coreman.core.db.models import RelayServer
from tests.api.conftest import MASTER_KEY, login_as
from tests.fakes.runtime_node import add_node_relay

LEGACY_FIELDS = {
    "host",
    "ssh_user",
    "runtime_env",
    "chroot_path",
    "runtime_user",
    "clawrelay_port",
    "agent_port",
    "webhook_port",
}


async def test_agent_token_is_bound_to_server_and_telemetry_preserves_version(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    cipher = Cipher(base64.b64decode(MASTER_KEY))
    token = "synthetic-agent-token-long-enough"
    relay = await add_node_relay(
        db_session,
        name="report1",
        agent_token_enc=cipher.encrypt(token, "relay_servers.agent_token_enc"),
    )
    other = await add_node_relay(db_session, name="report2")
    await db_session.commit()
    version = relay.version
    headers = {"Authorization": f"Bearer {token}"}
    quota = {
        "server_id": str(relay.id),
        "rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": "2026-09-13T10:00:00Z"}},
    }
    r = await client.post("/api/infra/relay/rate-limits", json=quota, headers=headers)
    assert r.status_code == 200, r.text
    await db_session.refresh(relay)
    assert relay.rate_limit_5h_used_pct == Decimal(95) and relay.version == version
    assert (
        await client.post(
            "/api/infra/relay/rate-limits",
            json={**quota, "server_id": str(other.id)},
            headers=headers,
        )
    ).status_code == 401
    # 目标只能按 server_id 指定；空额度保留上次结果。
    assert (
        await client.post("/api/infra/relay/rate-limits", json={"rate_limits": {}}, headers=headers)
    ).status_code == 422
    r = await client.post(
        "/api/infra/relay/rate-limits",
        json={"server_id": str(relay.id), "rate_limits": {}},
        headers=headers,
    )
    assert r.status_code == 200
    r = await client.post(
        "/api/infra/relay/health",
        json={"server_id": str(relay.id), "status": "down"},
        headers=headers,
    )
    assert r.status_code == 200
    await db_session.refresh(relay)
    assert (
        relay.rate_limit_5h_used_pct == Decimal(95)
        and relay.health_fail_count == 1
        and relay.version == version
    )
    assert (await client.get("/api/infra/relay/servers", headers=headers)).status_code == 401
    for legacy in ("/api/robot/rate-limits/report", "/api/robot/health/report"):
        assert (await client.post(legacy, json=quota, headers=headers)).status_code in {404, 405}


async def test_signed_report_and_server_list(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="platform_admin")
    data = (
        await client.post(
            "/api/admin/api-clients",
            json={"app_key": "reporter", "name": "Reporter", "scopes": ["relay"]},
        )
    ).json()["data"]
    relay = await add_node_relay(db_session, name="signed")
    db_session.add(RelayServer(name="standalone", model_provider="claude"))
    await db_session.commit()

    def signed(method: str, path: str, body: dict[str, object]) -> dict[str, str]:
        ts = str(int(time.time()))
        return {
            "X-App-Key": "reporter",
            "X-Timestamp": ts,
            "X-Signature": sign_request(method, path, body, ts, "reporter", data["secret"]),
        }

    path = "/api/infra/relay/health"
    body = {"server_id": str(relay.id), "status": "healthy", "latency_ms": 123}
    headers = signed("POST", path, body)
    assert (await client.post(path, json=body, headers=headers)).status_code == 200
    assert (
        await client.post(path, json={**body, "status": "down"}, headers=headers)
    ).status_code == 401
    await db_session.refresh(relay)
    assert relay.health_status == "healthy" and relay.health_latency_ms == 123

    listing = "/api/infra/relay/servers"
    rows = (await client.get(listing, headers=signed("GET", listing, {}))).json()["data"]
    assert [row["id"] for row in rows] == [str(relay.id)]
    assert rows[0]["runtime_node_id"] == str(relay.runtime_node_id)
    assert not LEGACY_FIELDS & set(rows[0])


def test_client_metadata_is_allowlisted_and_keeps_its_own_time() -> None:
    from coreman.api.routers.infra_relay import client_metadata

    now = datetime.now(UTC)
    raw = {
        "claude": {
            "installed": True,
            "login": "ready",
            "version": "1.0",
            "account": "private",
            "token": "private",
            "seats": ["private"],
        }
    }
    assert client_metadata(raw, "claude", now) == {
        "installed": True,
        "login": "ready",
        "version": "1.0",
        "observed_at": now,
    }
    assert client_metadata(raw, "opencode", now) == {}
    assert client_metadata(None, "claude", now) == {}


async def test_signed_list_projects_node_capabilities(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from tests.fakes.runtime_node import add_node

    await login_as(client, db_session, role="platform_admin")
    credentials = (
        await client.post(
            "/api/admin/api-clients",
            json={"app_key": "metadata-reader", "name": "Metadata reader", "scopes": ["relay"]},
        )
    ).json()["data"]
    now = datetime.now(UTC)
    node = await add_node(
        db_session,
        heartbeat_at=now,
        capabilities={
            "opencode": {
                "installed": True,
                "login": "required",
                "version": "1.2",
                "account": "private",
            }
        },
    )
    relay = await add_node_relay(db_session, node=node, provider="opencode")
    await db_session.commit()
    path = "/api/infra/relay/servers"
    ts = str(int(time.time()))
    response = await client.get(
        path,
        headers={
            "X-App-Key": "metadata-reader",
            "X-Timestamp": ts,
            "X-Signature": sign_request(
                "GET", path, {}, ts, "metadata-reader", credentials["secret"]
            ),
        },
    )
    assert response.status_code == 200
    rows = response.json()["data"]
    metadata = next(r["client_metadata"] for r in rows if r["id"] == str(relay.id))
    assert metadata["installed"] is True
    assert metadata["login"] == "required" and metadata["version"] == "1.2"
    assert datetime.fromisoformat(metadata["observed_at"].replace("Z", "+00:00")) == now
    assert set(metadata) == {"installed", "login", "version", "observed_at"}
