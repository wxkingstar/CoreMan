"""迁移 0043：新增 max 档位，按官方文档改正目录的 xhigh / max 标记，并给受影响的机器人降档。"""

from __future__ import annotations

import asyncio

from alembic import command

from tests.conftest import alembic_config
from tests.integration.test_migration_native_model_names import _execute, _restore_seed, _rows

INSERT_MODEL = (
    "INSERT INTO model_catalog (provider, model, supports_xhigh) VALUES (:provider, :model, :xhigh)"
)
INSERT_BOT = (
    "INSERT INTO bots (bot_key, platform, name, created_by, model, working_dir, effort_level,"
    " credentials_enc) SELECT :key, 'wecom', :key, id, :model, '/data/x', :effort, ''"
    " FROM users WHERE login_name = 'migration-owner'"
)
CLEANUP = ("TRUNCATE bots, users, settings CASCADE", {})


def test_0043_sets_known_effort_support_and_downgrades_bots(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    command.downgrade(cfg, "0042")
    try:
        asyncio.run(
            _execute(
                migrated_database,
                ("TRUNCATE model_catalog", {}),
                (INSERT_MODEL, {"provider": "claude", "model": "claude-opus-5", "xhigh": False}),
                (
                    INSERT_MODEL,
                    {"provider": "claude", "model": "claude-opus-4-6", "xhigh": True},
                ),
                (
                    INSERT_MODEL,
                    {"provider": "claude", "model": "claude-haiku-4-5-20251001", "xhigh": True},
                ),
                (INSERT_MODEL, {"provider": "codex", "model": "codex/gpt-5.5", "xhigh": False}),
                # 文档里没有的模型保持管理员的设置。
                (INSERT_MODEL, {"provider": "claude", "model": "kimi/kimi-k2.5", "xhigh": True}),
                (
                    "INSERT INTO users (login_name, display_name) "
                    "VALUES ('migration-owner', 'Owner')",
                    {},
                ),
                (INSERT_BOT, {"key": "old_opus", "model": "claude-opus-4-6", "effort": "xhigh"}),
                (INSERT_BOT, {"key": "old_opus_hi", "model": "claude-opus-4-6", "effort": "high"}),
                (INSERT_BOT, {"key": "kimi", "model": "kimi/kimi-k2.5", "effort": "xhigh"}),
            )
        )
        command.upgrade(cfg, "head")
        catalog = asyncio.run(
            _rows(
                migrated_database,
                "SELECT provider, model, supports_xhigh, supports_max FROM model_catalog"
                " ORDER BY 1, 2",
            )
        )
        bots = asyncio.run(
            _rows(migrated_database, "SELECT bot_key, effort_level, version FROM bots ORDER BY 1")
        )
        # 新约束接受 max。
        asyncio.run(
            _execute(
                migrated_database,
                (INSERT_BOT, {"key": "opus_max", "model": "claude-opus-5", "effort": "max"}),
            )
        )
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_execute(migrated_database, CLEANUP))
        asyncio.run(_restore_seed(migrated_database))

    assert catalog == [
        ("claude", "claude-haiku-4-5-20251001", False, False),
        ("claude", "claude-opus-4-6", False, True),
        ("claude", "claude-opus-5", True, True),
        ("claude", "kimi/kimi-k2.5", True, False),
        ("codex", "codex/gpt-5.5", True, False),
    ]
    assert bots == [("kimi", "xhigh", 1), ("old_opus", "high", 2), ("old_opus_hi", "high", 1)]


def test_0043_downgrade_maps_max_back_to_supported_levels(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    try:
        asyncio.run(
            _execute(
                migrated_database,
                (
                    "INSERT INTO users (login_name, display_name) "
                    "VALUES ('migration-owner', 'Owner')",
                    {},
                ),
                # 种子里 Opus 5 支持 xhigh，Haiku 4.5 不支持。
                (INSERT_BOT, {"key": "opus", "model": "claude-opus-5", "effort": "max"}),
                (
                    INSERT_BOT,
                    {"key": "haiku", "model": "claude-haiku-4-5-20251001", "effort": "max"},
                ),
                (
                    "INSERT INTO settings (key, value) VALUES ('default_effort_level', '\"max\"')"
                    " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    {},
                ),
            )
        )
        command.downgrade(cfg, "0042")
        bots = asyncio.run(
            _rows(migrated_database, "SELECT bot_key, effort_level FROM bots ORDER BY 1")
        )
        default = asyncio.run(
            _rows(
                migrated_database,
                "SELECT value #>> '{}' FROM settings WHERE key = 'default_effort_level'",
            )
        )
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_execute(migrated_database, CLEANUP))
        asyncio.run(_restore_seed(migrated_database))

    assert bots == [("haiku", "high"), ("opus", "xhigh")]
    assert default == [("xhigh",)]
