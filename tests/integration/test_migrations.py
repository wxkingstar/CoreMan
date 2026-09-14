from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import alembic_config

EXPECTED_TABLES = {
    "teams",
    "users",
    "admin_sessions",
    "audit_logs",
    "settings",
    "alembic_version",
    "team_rules",
    "user_identities",
    "departments",
    "user_departments",
    "auth_nonces",
    "login_attempts",
    "platform_apps",
    "contact_sync_runs",
    "relay_servers",
    "model_catalog",
    "bots",
    "bot_members",
    "bot_allowed_users",
}


async def test_baseline_creates_tables(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
    assert EXPECTED_TABLES <= names


async def test_updated_at_trigger(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO teams (slug, name_zh) VALUES ('backoffice', '中后台')")
        )
        before = (await conn.execute(text("SELECT updated_at FROM teams"))).scalar_one()
        await conn.execute(text("SELECT pg_sleep(0.05)"))
        await conn.execute(text("UPDATE teams SET name_zh = '中后台2'"))
        after = (await conn.execute(text("SELECT updated_at FROM teams"))).scalar_one()
    assert after > before


def test_models_match_migrations(migrated_database: str) -> None:
    """等价于 CI 里的 alembic check：模型与迁移一致才通过。"""
    command.check(alembic_config(migrated_database))
