from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.relay.observations import HealthObservation, save_health
from coreman.core.relay.quota import Quota, QuotaWindow, save_quota
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
            detail="Your Claude session has expired. Sign in again to continue",
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
    "provider,plan,verified",
    [
        ("codex", "pro", None),
        ("codex", "pro_100", None),
        ("codex", "pro_200", None),
        ("codex", "pro_500", None),
        ("claude", "ultra", True),
        ("claude", "ultra", False),
        ("claude", "max_20x", True),
        ("claude", "max", True),
    ],
)
async def test_missing_window_remains_unknown_despite_plan_aliases(
    db_session, provider, plan, verified
):
    relay = await add_node_relay(db_session, provider=provider)
    await save_quota(
        db_session,
        relay.id,
        Quota(plan_type=plan, account_verified=verified, seven_day=QuotaWindow(used_percentage=17)),
    )
    await db_session.refresh(relay)
    assert relay.quota_observation["windows"]["five_hour"]["applicability"] == "unknown"
    assert relay.rate_limit_5h_used_pct is None
    assert relay.rate_limit_7d_used_pct == 17


async def test_real_window_wins_over_explicit_applicability_and_has_own_time(db_session):
    relay = await add_node_relay(db_session, provider="codex")
    now = datetime.now(UTC)
    older = now - timedelta(minutes=30)
    await save_quota(
        db_session,
        relay.id,
        Quota(
            plan_type="pro",
            window_applicability={"five_hour": "not_applicable"},
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


async def test_only_explicit_upstream_policy_marks_window_not_applicable(db_session):
    relay = await add_node_relay(db_session, provider="codex")
    await save_quota(
        db_session, relay.id, Quota(window_applicability={"five_hour": "not_applicable"})
    )
    await db_session.refresh(relay)
    assert relay.quota_observation["windows"]["five_hour"]["applicability"] == "not_applicable"
    assert relay.rate_limit_5h_used_pct is None


async def test_server_receipt_time_does_not_order_dated_node_windows(db_session):
    relay = await add_node_relay(db_session)
    node_at = datetime.now(UTC) - timedelta(minutes=5)
    await save_quota(db_session, relay.id, Quota(seven_day=QuotaWindow(used_percentage=90)))
    await save_quota(
        db_session,
        relay.id,
        Quota(observed_at=node_at, seven_day=QuotaWindow(used_percentage=15)),
    )
    # A late, older upstream window cannot replace the accepted node reading.
    await save_quota(
        db_session,
        relay.id,
        Quota(
            seven_day=QuotaWindow(used_percentage=90, observed_at=node_at - timedelta(seconds=1))
        ),
    )
    await db_session.refresh(relay)
    assert relay.rate_limit_7d_used_pct == 15
    assert relay.quota_observation["windows"]["seven_day"]["observed_at"] == node_at.isoformat()


async def test_reply_does_not_freshen_a_cached_window_carried_with_a_new_window(db_session):
    relay = await add_node_relay(db_session)
    now = datetime.now(UTC)
    older = now - timedelta(hours=2)
    cached = QuotaWindow(used_percentage=24, observed_at=older)
    await save_quota(db_session, relay.id, Quota(five_hour=cached, observed_at=older))
    await save_quota(
        db_session,
        relay.id,
        Quota(
            five_hour=cached,
            seven_day=QuotaWindow(used_percentage=16),
            observed_at=now,
            source="conversation",
        ),
    )
    await db_session.refresh(relay)
    assert relay.rate_limit_probed_at == older
    assert relay.quota_observation["windows"]["five_hour"]["observed_at"] == older.isoformat()
    assert relay.quota_observation["windows"]["seven_day"]["observed_at"] == now.isoformat()


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


@pytest.mark.parametrize("kind", ["legacy", "driver", "model", "conversation"])
async def test_legacy_reports_without_seat_id_work_with_advertised_active_seat(db_session, kind):
    node = await add_node(
        db_session, capabilities={"codex": {"seats": [{"id": "aaaaaaaaaaaa", "active": True}]}}
    )
    relay = await add_node_relay(db_session, node=node, provider="codex")
    await save_health(
        db_session,
        relay.id,
        HealthObservation(status="healthy", check_kind=kind, reply_verified=True),
    )
    await save_quota(db_session, relay.id, Quota(seven_day=QuotaWindow(used_percentage=23)))
    await db_session.refresh(relay)
    assert relay.health_status == "healthy"
    assert relay.rate_limit_7d_used_pct == 23
    if kind in {"model", "conversation"}:
        assert relay.model_health["reply_verified"] is True
