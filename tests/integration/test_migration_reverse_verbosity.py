"""迁移 0053：输出详细度改为 1 极简 … 4 详细（模型默认），机器人档位与改写过的提示词跟着换。"""

from __future__ import annotations

import asyncio
import importlib
import json

from alembic import command

from coreman.core.prompting.defaults import DEFAULT_VERBOSITY
from tests.conftest import alembic_config
from tests.integration.test_migration_native_model_names import _execute, _rows

migration = importlib.import_module("migrations.versions.0053_reverse_verbosity_levels")

INSERT_BOT = (
    "INSERT INTO bots (bot_key, platform, name, created_by, model, working_dir, verbosity_level,"
    " credentials_enc) SELECT :key, 'wecom', :key, id, 'codex/gpt-6-sol', '/data/x', :level, ''"
    " FROM users WHERE login_name = 'migration-owner'"
)
INSERT_DEFAULT_BOT = (
    "INSERT INTO bots (bot_key, platform, name, created_by, model, working_dir, credentials_enc)"
    " SELECT :key, 'wecom', :key, id, 'codex/gpt-6-sol', '/data/x', ''"
    " FROM users WHERE login_name = 'migration-owner'"
)
INSERT_SETTING = "INSERT INTO settings (key, value) VALUES (:key, CAST(:value AS jsonb))"
SETTINGS = (
    "SELECT key, value FROM settings WHERE key = 'default_verbosity_level'"
    " OR key LIKE 'prompt_verbosity_%' ORDER BY key"
)
CLEANUP = (
    "DELETE FROM settings WHERE key = 'default_verbosity_level' OR key LIKE 'prompt_verbosity_%'",
    {},
)


def _setting(key: str, value: object) -> tuple[str, dict[str, object]]:
    return INSERT_SETTING, {"key": key, "value": json.dumps(value)}


def test_0053_factory_text_matches_code() -> None:
    """downgrade 靠它识别「仍是出厂文案」的覆盖值，必须与代码里的默认值一致。"""
    assert migration.NEW_FACTORY == DEFAULT_VERBOSITY


def test_0053_reverses_levels_and_moves_prompt_overrides(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    command.downgrade(cfg, "0052")
    try:
        asyncio.run(
            _execute(
                migrated_database,
                CLEANUP,
                (
                    "INSERT INTO users (login_name, display_name) "
                    "VALUES ('migration-owner', 'Owner')",
                    {},
                ),
                *((INSERT_BOT, {"key": f"old{n}", "level": n}) for n in (1, 2, 3, 4)),
                _setting("default_verbosity_level", 1),
                # 仍是旧出厂文案：删掉，让新出厂文案生效。
                _setting("prompt_verbosity_2", migration.OLD_FACTORY[2]),
                _setting("prompt_verbosity_3", "像同事一样说话"),
                _setting("prompt_verbosity_4", "只给结论"),
            )
        )
        command.upgrade(cfg, "0053")
        asyncio.run(_execute(migrated_database, (INSERT_DEFAULT_BOT, {"key": "fresh"})))
        bots = asyncio.run(
            _rows(
                migrated_database, "SELECT bot_key, verbosity_level, version FROM bots ORDER BY 1"
            )
        )
        settings = asyncio.run(_rows(migrated_database, SETTINGS))
        command.downgrade(cfg, "0052")
        bots_down = asyncio.run(
            _rows(migrated_database, "SELECT bot_key, verbosity_level FROM bots ORDER BY 1")
        )
        settings_down = asyncio.run(_rows(migrated_database, SETTINGS))
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_execute(migrated_database, ("TRUNCATE bots, users CASCADE", {}), CLEANUP))

    # 每个机器人行为不变：n → 5 − n；没写档位的新机器人落在 4 档（模型默认）。
    assert bots == [("fresh", 4, 1), ("old1", 4, 2), ("old2", 3, 2), ("old3", 2, 2), ("old4", 1, 2)]
    assert settings == [
        ("default_verbosity_level", 4),
        ("prompt_verbosity_1", "只给结论"),
        ("prompt_verbosity_2", "像同事一样说话"),
    ]
    assert bots_down == [("fresh", 1), ("old1", 1), ("old2", 2), ("old3", 3), ("old4", 4)]
    assert settings_down == [
        ("default_verbosity_level", 1),
        ("prompt_verbosity_3", "像同事一样说话"),
        ("prompt_verbosity_4", "只给结论"),
    ]
