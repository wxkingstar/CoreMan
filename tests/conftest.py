"""测试数据库：优先 TEST_DATABASE_URL（CI 的 postgres service），

否则用 testcontainers 起 postgres:16。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.db.session import make_engine, make_session_factory

ROOT = Path(__file__).resolve().parents[1]
BUSINESS_TABLES = [
    "bot_collaborations", "bot_collaboration_routes",
    "runtime_chunks", "runtime_calls", "runtime_install_links", "runtime_nodes",
    "alert_states",
    "feishu_deliveries",
    "model_prices",
    "bot_skills",
    "skill_approvals",
    "memories",
    "skills",
    "skill_sources",
    "env_presets",
    "stored_objects",
    "cron_runs",
    "cron_jobs",
    "escalations",
    "bot_system_grants",
    "system_grant_audit",
    "systems",
    "api_clients",
    "jwt_keys",
    "interaction_states",
    "announcements",
    "chat_logs",
    "user_reached",
    "chat_sessions",
    "outbox",
    "task_streams",
    "tasks",
    "inbound_events",
    "bot_leases",
    "process_instances",
    "bot_allowed_users",
    "bot_members",
    "bots",
    "relay_servers",
    "model_catalog",
    "contact_sync_runs",
    "platform_apps",
    "login_attempts",
    "auth_nonces",
    "user_departments",
    "departments",
    "user_identities",
    "team_rules",
    "audit_logs",
    "admin_sessions",
    "settings",
    "users",
    "teams",
]


SEED_MODEL_CATALOG = text(
    "INSERT INTO model_catalog (provider, model, display_name, is_default, sort_order)"
    " VALUES (:provider, :model, :display_name, :is_default, :sort_order)"
    " ON CONFLICT DO NOTHING"
)


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = database_url
    return cfg


def model_catalog_seed() -> list[dict[str, object]]:
    """迁移 0003 里的模型目录种子（只读脚本目录，不连库）。

    `model_catalog` 也列在 BUSINESS_TABLES 里（用例可以增删目录行，不清理会串味），但它同时
    是迁移写入的参考数据；TRUNCATE 之后按这份唯一来源补回，每个用例才都从「迁移后」状态起跑。
    """
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    revision = ScriptDirectory.from_config(cfg).get_revision("0003")
    assert revision is not None
    return [
        {"provider": p, "model": m, "display_name": d, "is_default": df, "sort_order": s}
        for p, m, d, df, s in revision.module.SEED_MODELS
    ]


@pytest.fixture(scope="session", autouse=True)
def _ignore_dotenv() -> Iterator[None]:
    """测试不得受本地 .env 影响。

    Settings 启动时读取 .env 是产品行为（README 承诺），但开发机 worktree 根目录的真实
    .env 会让 tests/unit/test_config.py::test_defaults 之类的用例读到本机配置而失败。
    pydantic-settings 在实例化时才读 model_config["env_file"]，运行期改它即可全局隔离。
    """
    from coreman.core.config import Settings

    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    yield
    Settings.model_config["env_file"] = original


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    cfg = alembic_config(database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return database_url


@pytest.fixture
async def db_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(migrated_database)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text(f"TRUNCATE {', '.join(BUSINESS_TABLES)} RESTART IDENTITY CASCADE")
            )
            await conn.execute(SEED_MODEL_CATALOG, model_catalog_seed())
        await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = make_session_factory(db_engine)
    async with factory() as session:
        yield session


@pytest.fixture(autouse=True)
def _isolate_network_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep local fake services independent of developer shell proxy settings."""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
