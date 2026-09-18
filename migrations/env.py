"""async Alembic 环境。

URL 优先级：config.attributes["database_url"] > 环境变量 DATABASE_URL > alembic.ini。
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from coreman.core.db import models  # noqa: F401  注册全部模型
from coreman.core.db.base import Base

config = context.config
if config.config_file_name is not None:
    # fileConfig disables every logger that already exists by default; when migrations run
    # in-process (tests' migrated_database fixture) that silences e.g. "coreman-runtime".
    fileConfig(config.config_file_name, disable_existing_loggers=False)

_url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL")
if _url:
    config.set_main_option("sqlalchemy.url", _url)

target_metadata = Base.metadata

# "alembic.ext.checkconstraint_byname" is opt-in as of Alembic 1.19.2 (not
# matched by the "alembic.autogenerate.*" wildcard on purpose, see
# alembic/runtime/plugins.py); it must be listed explicitly alongside the
# wildcard to keep the default comparators (tables/columns/types/etc.) while
# adding CHECK-constraint comparison.
AUTOGENERATE_PLUGINS = ["alembic.autogenerate.*", "alembic.ext.checkconstraint_byname"]


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_server_default=True,
        autogenerate_plugins=AUTOGENERATE_PLUGINS,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        autogenerate_plugins=AUTOGENERATE_PLUGINS,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
