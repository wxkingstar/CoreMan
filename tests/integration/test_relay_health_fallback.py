from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from coreman.core.db.models import RelayServer
from coreman.core.db.session import make_session_factory
from coreman.core.observability import relay_health
from tests.integration.worker_helpers import seed_bot


async def test_health_fallback_skips_fresh_and_does_not_overwrite_new_report(
    db_engine, db_session, monkeypatch
):
    _, relay, _ = await seed_bot(db_session)
    now = datetime.now(UTC)
    relay.health_checked_at = now
    await db_session.commit()
    factory = make_session_factory(db_engine)
    calls = []

    async def healthy(snapshot):
        calls.append(snapshot.id)
        return "healthy", 5

    monkeypatch.setattr(relay_health, "read_health", healthy)
    assert await relay_health.tick(factory, now) == 0 and not calls
    assert await relay_health.tick(factory, now + timedelta(hours=3)) == 1
    await db_session.refresh(relay)
    assert relay.health_status == "healthy"

    async def raced(snapshot):
        async with factory() as session:
            await session.execute(
                update(RelayServer)
                .where(RelayServer.id == snapshot.id)
                .values(health_checked_at=now + timedelta(hours=6), health_status="auth_fail")
            )
            await session.commit()
        return "healthy", 8

    monkeypatch.setattr(relay_health, "read_health", raced)
    assert await relay_health.tick(factory, now + timedelta(hours=6)) == 0
    await db_session.refresh(relay)
    assert relay.health_status == "auth_fail"
