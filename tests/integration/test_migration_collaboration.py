"""Upgrade a pre-main QA database without recreating its collaboration ledger."""

import asyncio

from alembic import command
from alembic.script import ScriptDirectory

from tests.conftest import alembic_config
from tests.integration.test_migration_native_model_names import _execute, _restore_seed, _rows


def test_legacy_0023_keeps_ledger_and_applies_missed_model_rename(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    # Reconstruct the historical snapshot before changing its version stamp.
    command.downgrade(cfg, "0026")
    before = asyncio.run(_rows(migrated_database, "SELECT 'bot_collaborations'::regclass::oid"))
    # Same tables as the QA revision, but its 0023 stamp predates the main data migration.
    command.stamp(cfg, "0023")
    try:
        asyncio.run(
            _execute(
                migrated_database,
                (
                    "UPDATE model_catalog SET model = 'vllm/claude-sonnet-4-6' "
                    "WHERE model = 'claude-sonnet-4-6'",
                    {},
                ),
            )
        )
        command.upgrade(cfg, "head")
        assert asyncio.run(_rows(migrated_database, "SELECT version_num FROM alembic_version")) == [
            (head,)
        ]
        assert (
            asyncio.run(_rows(migrated_database, "SELECT 'bot_collaborations'::regclass::oid"))
            == before
        )
        assert asyncio.run(
            _rows(
                migrated_database,
                "SELECT count(*) FROM model_catalog WHERE model = 'vllm/claude-sonnet-4-6'",
            )
        ) == [(0,)]
        assert asyncio.run(
            _rows(
                migrated_database,
                "SELECT count(*) FROM model_catalog WHERE model = 'claude-sonnet-4-6'",
            )
        ) == [(1,)]
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_restore_seed(migrated_database))


def test_reactions_only_0025_gets_missing_collaboration_columns(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    # Reconstruct the historical snapshot before changing its version stamp.
    command.downgrade(cfg, "0026")
    before = asyncio.run(_rows(migrated_database, "SELECT 'bot_collaborations'::regclass::oid"))
    command.stamp(cfg, "0025")
    try:
        asyncio.run(
            _execute(
                migrated_database,
                (
                    "ALTER TABLE bot_collaboration_routes DROP COLUMN setup, "
                    "DROP COLUMN archived, DROP COLUMN version",
                    {},
                ),
            )
        )
        command.upgrade(cfg, "head")
        assert asyncio.run(_rows(migrated_database, "SELECT version_num FROM alembic_version")) == [
            (head,)
        ]
        assert (
            asyncio.run(_rows(migrated_database, "SELECT 'bot_collaborations'::regclass::oid"))
            == before
        )
        assert asyncio.run(
            _rows(
                migrated_database,
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='bot_collaboration_routes' "
                "AND column_name IN ('setup','archived','version')",
            )
        ) == [(3,)]
        assert asyncio.run(
            _rows(
                migrated_database,
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='feishu_deliveries' AND column_name LIKE 'reaction_%'",
            )
        ) == [(4,)]
    finally:
        command.upgrade(cfg, "head")
