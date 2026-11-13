"""app_leases：助理应用的连接租约，规则与 bot_leases 同源。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import app_leases, instances
from coreman.core.db.models import AppLease, PlatformApp


async def _apps(session: AsyncSession) -> tuple[PlatformApp, PlatformApp, PlatformApp]:
    on = PlatformApp(
        platform="feishu",
        name="个人助理",
        capabilities=["assistant"],
        app_id="cli_example",
        secret_enc="x",
    )
    off = PlatformApp(
        platform="feishu",
        name="停用的",
        capabilities=["assistant"],
        app_id="cli_off",
        secret_enc="x",
        enabled=False,
    )
    login = PlatformApp(
        platform="feishu", name="登录", capabilities=["login"], app_id="cli_login", secret_enc="x"
    )
    session.add_all([on, off, login])
    for name in ("gw-a", "gw-b"):
        await instances.register(
            session, instance_id=name, service="gateway-feishu", version="dev", capacity=None
        )
    await session.commit()
    return on, off, login


async def test_only_assistant_apps_get_rows_and_only_enabled_are_acquirable(
    db_session: AsyncSession,
) -> None:
    on, off, login = await _apps(db_session)
    await app_leases.ensure_rows(db_session)
    await app_leases.ensure_rows(db_session)  # 幂等
    await db_session.commit()
    assert await db_session.get(AppLease, login.id) is None
    assert await db_session.get(AppLease, off.id) is not None
    assert await app_leases.acquirable(db_session) == [on.id]


async def test_acquire_heartbeat_release_and_takeover(db_session: AsyncSession) -> None:
    on, _, _ = await _apps(db_session)
    await app_leases.ensure_rows(db_session)
    assert await app_leases.acquire(db_session, on.id, "gw-a") == 1
    await db_session.commit()
    assert await app_leases.acquire(db_session, on.id, "gw-b") is None
    assert await app_leases.acquirable(db_session) == []
    assert await app_leases.heartbeat(db_session, "gw-a") == 1
    await app_leases.set_state(db_session, on.id, "subscribed", instance_id="gw-a")
    await app_leases.set_state(db_session, on.id, "kicked", instance_id="gw-b")  # 非持有者无效
    await db_session.commit()
    held = await app_leases.held_by(db_session, "gw-a")
    assert [row.key for row in held] == [on.id] and held[0].connection_state == "subscribed"
    # 心跳过期后别的实例可以接管，代次 +1。
    held[0].heartbeat_at = datetime.now(UTC) - timedelta(seconds=31)
    await db_session.commit()
    assert await app_leases.acquire(db_session, on.id, "gw-b") == 2
    await db_session.commit()
    # 上一代迟到的释放不生效；按当前代次释放才生效。
    await app_leases.release(db_session, on.id, "gw-b", 1)
    await db_session.commit()
    assert [row.key for row in await app_leases.held_by(db_session, "gw-b")] == [on.id]
    await app_leases.release(db_session, on.id, "gw-b", 2)
    await db_session.commit()
    row = await db_session.get(AppLease, on.id, populate_existing=True)
    assert row is not None and row.holder_instance is None
    assert row.connection_state == "disconnected"


async def test_disabled_among_reports_apps_that_stopped_being_serviceable(
    db_session: AsyncSession,
) -> None:
    on, off, login = await _apps(db_session)
    assert await app_leases.disabled_among(db_session, []) == set()
    assert await app_leases.disabled_among(db_session, [on.id, off.id, login.id]) == {
        off.id,
        login.id,
    }


async def test_rows_go_away_with_the_app(db_session: AsyncSession) -> None:
    on, _, _ = await _apps(db_session)
    await app_leases.ensure_rows(db_session)
    await db_session.commit()
    await db_session.delete(on)
    await db_session.commit()
    assert await db_session.get(AppLease, on.id) is None
