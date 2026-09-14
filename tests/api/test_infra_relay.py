import base64
import time
from decimal import Decimal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.signatures import sign_request
from coreman.core.crypto import Cipher
from coreman.core.db.models import RelayServer
from tests.api.conftest import MASTER_KEY, login_as


async def test_agent_token_is_bound_to_server_and_telemetry_preserves_version(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    cipher = Cipher(base64.b64decode(MASTER_KEY))
    token = "synthetic-agent-token-long-enough"
    relay = RelayServer(
        name="report1",
        host="10.0.0.1",
        clawrelay_port=5000,
        agent_port=5001,
        model_provider="claude",
        agent_token_enc=cipher.encrypt(token, "relay_servers.agent_token_enc"),
    )
    other = RelayServer(
        name="report2",
        host="10.0.0.2",
        clawrelay_port=5000,
        agent_port=5001,
        model_provider="claude",
    )
    db_session.add_all([relay, other])
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
    # 旧上报按 agent 端口 + 名称匹配。空额度保留上次结果。
    r = await client.post(
        "/api/robot/rate-limits/report",
        json={"port": 5001, "name": "report1", "rate_limits": {}},
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


async def test_signed_legacy_report_and_invalid_percentage(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="platform_admin")
    data = (
        await client.post(
            "/api/admin/api-clients",
            json={"app_key": "reporter", "name": "Reporter", "scopes": ["relay"]},
        )
    ).json()["data"]
    relay = RelayServer(
        name="signed",
        host="10.0.0.3",
        clawrelay_port=5000,
        agent_port=5001,
        model_provider="claude",
    )
    db_session.add(relay)
    await db_session.commit()
    path = "/api/robot/health/report"
    body = {"port": 5001, "name": "signed", "status": "healthy", "latency_ms": 123}
    ts = str(int(time.time()))
    headers = {
        "X-App-Key": "reporter",
        "X-Timestamp": ts,
        "X-Signature": sign_request("POST", path, body, ts, "reporter", data["secret"]),
    }
    assert (await client.post(path, json=body, headers=headers)).status_code == 200
    assert (
        await client.post(path, json={**body, "status": "down"}, headers=headers)
    ).status_code == 401
    await db_session.refresh(relay)
    assert relay.health_status == "healthy" and relay.health_latency_ms == 123
