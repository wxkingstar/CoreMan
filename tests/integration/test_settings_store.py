from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from coreman.core.db.models import Setting
from coreman.core.db.session import make_session_factory
from coreman.core.settings_store import SettingsStore


async def test_get_default_then_set_then_cached(db_engine: AsyncEngine) -> None:
    now = [1000.0]
    store = SettingsStore(make_session_factory(db_engine), ttl_seconds=60, clock=lambda: now[0])
    assert await store.get("bootstrap_admin_enabled", default=True) is True
    await store.set("bootstrap_admin_enabled", False)
    assert await store.get("bootstrap_admin_enabled", default=True) is False

    other = SettingsStore(make_session_factory(db_engine), ttl_seconds=60, clock=lambda: now[0])
    assert await other.get("bootstrap_admin_enabled", default=True) is False
    await store.set("bootstrap_admin_enabled", True)
    assert await other.get("bootstrap_admin_enabled", default=True) is False  # 60 秒缓存内仍旧值
    now[0] += 61
    assert await other.get("bootstrap_admin_enabled", default=True) is True


async def test_invalidate_and_json_values(db_engine: AsyncEngine) -> None:
    store = SettingsStore(make_session_factory(db_engine))
    await store.set("alert_channels", {"wecom_app": ["u1"], "email": []})
    store.invalidate()
    assert await store.get("alert_channels") == {"wecom_app": ["u1"], "email": []}


async def test_stored_json_null_is_not_missing(db_engine: AsyncEngine) -> None:
    store = SettingsStore(make_session_factory(db_engine))
    await store.set("nullable_key", None)
    store.invalidate()
    assert await store.get("nullable_key", default="fallback") is None
    assert await store.get("absent_key", default="fallback") == "fallback"


async def test_set_refreshes_updated_at(db_engine: AsyncEngine) -> None:
    """settings 表没有触发器，server_default 只在 INSERT 生效：
    ON CONFLICT DO UPDATE 必须自己写 updated_at，否则改过的配置看着永远是初次写入时间。"""
    factory = make_session_factory(db_engine)
    store = SettingsStore(factory)

    async def row() -> Setting:
        async with factory() as session:
            return (
                await session.execute(select(Setting).where(Setting.key == "retry_max"))
            ).scalar_one()

    await store.set("retry_max", 3)
    first = (await row()).updated_at
    await store.set("retry_max", 5)
    second = await row()
    assert second.value == 5
    assert second.updated_at > first
