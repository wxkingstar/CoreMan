import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.signatures import sign_request
from coreman.core.relay.observations import HealthObservation, save_health
from coreman.core.relay.quota import Quota, QuotaWindow, save_quota
from tests.api.conftest import login_as
from tests.fakes.runtime_node import add_node, add_node_relay


async def test_driver_success_cannot_overwrite_model_failure(db_session: AsyncSession):
    relay = await add_node_relay(db_session)
    now = datetime.now(UTC)
    failed = now - timedelta(minutes=2)
    version = relay.version
    await save_health(
        db_session,
        relay.id,
        HealthObservation(
            status="auth_fail",
            check_kind="model",
            observed_at=failed,
            error_code="session_expired",
            driver_status="healthy",
        ),
    )
    await db_session.refresh(relay)
    assert relay.driver_health["status"] == "healthy"
    assert relay.model_health["status"] == "auth_fail"
    await save_health(
        db_session,
        relay.id,
        HealthObservation(
            status="healthy", check_kind="driver", observed_at=now, reply_verified=True
        ),
    )
    await db_session.refresh(relay)
    assert relay.driver_health["status"] == "healthy"
    assert relay.driver_health["reply_verified"] is False
    assert relay.model_health["status"] == "auth_fail"
    assert relay.last_model_failure["observed_at"] == failed.isoformat()
    assert relay.version == version
    await save_health(
        db_session,
        relay.id,
        HealthObservation(
            status="healthy", check_kind="conversation", observed_at=now, reply_verified=True
        ),
    )
    await db_session.refresh(relay)
    assert relay.model_health["reply_verified"] is True
    assert relay.last_model_failure["error_code"] == "session_expired"


async def test_late_health_report_does_not_replace_newer_model_result(db_session):
    relay = await add_node_relay(db_session)
    now = datetime.now(UTC)
    await save_health(
        db_session,
        relay.id,
        HealthObservation(
            status="healthy", check_kind="conversation", observed_at=now, reply_verified=True
        ),
    )
    await save_health(
        db_session,
        relay.id,
        HealthObservation(
            status="down", check_kind="model", observed_at=now - timedelta(minutes=1)
        ),
    )
    await db_session.refresh(relay)
    assert relay.model_health["status"] == "healthy"
    assert relay.last_model_failure is None


@pytest.mark.parametrize(
    "provider,plan,verified,expected",
    [
        ("codex", "pro", None, "not_applicable"),
        ("claude", "ultra", True, "not_applicable"),
        ("claude", "ultra", False, "unknown"),
        ("claude", "max", True, "unknown"),
    ],
)
async def test_quota_applicability_uses_explicit_plan_evidence(
    db_session, provider, plan, verified, expected
):
    relay = await add_node_relay(db_session, provider=provider)
    await save_quota(
        db_session,
        relay.id,
        Quota(plan_type=plan, account_verified=verified, seven_day=QuotaWindow(used_percentage=17)),
    )
    await db_session.refresh(relay)
    assert relay.quota_observation["windows"]["five_hour"]["applicability"] == expected
    assert relay.rate_limit_5h_used_pct is None
    assert relay.rate_limit_7d_used_pct == 17


async def test_real_window_wins_over_plan_default_and_has_own_time(db_session):
    relay = await add_node_relay(db_session, provider="codex")
    now = datetime.now(UTC)
    older = now - timedelta(minutes=30)
    await save_quota(
        db_session,
        relay.id,
        Quota(
            plan_type="pro",
            observed_at=now,
            five_hour=QuotaWindow(used_percentage=4, observed_at=older),
            seven_day=QuotaWindow(used_percentage=0),
        ),
    )
    await db_session.refresh(relay)
    windows = relay.quota_observation["windows"]
    assert windows["five_hour"]["applicability"] == "applicable"
    assert windows["five_hour"]["observed_at"] == older.isoformat()
    assert windows["seven_day"]["observed_at"] == now.isoformat()
    assert windows["seven_day"]["used_percentage"] == 0
    assert relay.rate_limit_probed_at == older
    assert relay.quota_observation["last_success_at"] == now.isoformat()


async def test_empty_and_failed_quota_reads_never_refresh_old_values(db_session):
    relay = await add_node_relay(db_session)
    older = datetime.now(UTC) - timedelta(hours=1)
    await save_quota(
        db_session, relay.id, Quota(observed_at=older, seven_day=QuotaWindow(used_percentage=54))
    )
    await save_quota(
        db_session, relay.id, Quota(read_status="error", error_code="quota_read_failed")
    )
    await db_session.refresh(relay)
    assert relay.rate_limit_probed_at == older
    assert relay.quota_observation["last_success_at"] == older.isoformat()
    assert relay.quota_observation["windows"]["seven_day"]["observed_at"] == older.isoformat()
    assert relay.quota_observation["read_status"] == "error"
    await save_quota(db_session, relay.id, Quota())
    await db_session.refresh(relay)
    assert relay.rate_limit_probed_at == older
    assert relay.rate_limit_7d_used_pct == 54


async def test_retired_seat_cannot_repaint_active_seat(db_session):
    node = await add_node(
        db_session, capabilities={"codex": {"seats": [{"id": "aaaaaaaaaaaa", "active": True}]}}
    )
    relay = await add_node_relay(db_session, node=node, provider="codex")
    await save_quota(
        db_session, relay.id, Quota(seat_id="default", seven_day=QuotaWindow(used_percentage=99))
    )
    await save_health(
        db_session,
        relay.id,
        HealthObservation(
            status="healthy", check_kind="conversation", reply_verified=True, seat_id="default"
        ),
    )
    await db_session.refresh(relay)
    assert relay.rate_limit_7d_used_pct is None and relay.model_health is None
    await save_quota(
        db_session,
        relay.id,
        Quota(seat_id="aaaaaaaaaaaa", seven_day=QuotaWindow(used_percentage=12)),
    )
    await db_session.refresh(relay)
    assert relay.rate_limit_7d_used_pct == 12


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
