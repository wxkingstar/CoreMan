"""迁移 0111：收件箱多一种 owner_said，设置多一列 focus_people_enc；降级去掉两者。"""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from sqlalchemy.exc import DBAPIError

from tests.conftest import alembic_config
from tests.integration.test_migration_native_model_names import _execute, _rows

USER = ("INSERT INTO users (login_name, display_name) VALUES ('mig-zhang', '张三')", {})
APP = (
    "INSERT INTO platform_apps (platform, name, capabilities, app_id, secret_enc)"
    " VALUES ('feishu', 'mig-assistant-app', ARRAY['assistant'], 'cli_example', 'x')",
    {},
)
BOT = (
    "INSERT INTO bots (bot_key, platform, name, created_by, owner_id, platform_app_id, kind,"
    " assistant_state, model, working_dir, credentials_enc)"
    " SELECT 'mig-a', 'feishu', '个人助理', u.id, u.id, a.id, 'assistant', 'active', 'm', '/w', ''"
    " FROM users u, platform_apps a WHERE u.login_name = 'mig-zhang'"
    " AND a.name = 'mig-assistant-app'",
    {},
)
OWNER_SAID = (
    "INSERT INTO assistant_inbox (bot_id, level, kind, message_id)"
    " SELECT id, 'batch', 'owner_said', 'om_example' FROM bots WHERE bot_key = 'mig-a'",
    {},
)
COLUMN = (
    "SELECT column_name FROM information_schema.columns"
    " WHERE table_name = 'assistant_settings' AND column_name = 'focus_people_enc'"
)


def test_0111_adds_the_kind_and_the_column_and_takes_them_back(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    try:
        assert asyncio.run(_rows(migrated_database, COLUMN)) == [("focus_people_enc",)]
        asyncio.run(_execute(migrated_database, USER, APP, BOT, OWNER_SAID))
        asyncio.run(_execute(migrated_database, ("DELETE FROM assistant_inbox", {})))
        command.downgrade(cfg, "0110")
        assert asyncio.run(_rows(migrated_database, COLUMN)) == []
        with pytest.raises(DBAPIError):
            asyncio.run(_execute(migrated_database, OWNER_SAID))
        command.upgrade(cfg, "head")
        assert asyncio.run(_rows(migrated_database, COLUMN)) == [("focus_people_enc",)]
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(
            _execute(migrated_database, ("TRUNCATE bots, users, platform_apps CASCADE", {}))
        )


def test_0111_downgrade_drops_owner_said_rows_instead_of_failing(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    try:
        asyncio.run(_execute(migrated_database, USER, APP, BOT, OWNER_SAID))
        command.downgrade(cfg, "0110")
        rows = asyncio.run(_rows(migrated_database, "SELECT kind FROM assistant_inbox"))
        assert rows == []
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(
            _execute(migrated_database, ("TRUNCATE bots, users, platform_apps CASCADE", {}))
        )
