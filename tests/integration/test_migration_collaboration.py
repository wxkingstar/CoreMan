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


def test_partner_migration_groups_legacy_routes_and_preserves_ledger(
    migrated_database: str,
) -> None:
    from tests.conftest import BUSINESS_TABLES

    async def seed():
        from coreman.core.db.session import make_engine, make_session_factory
        from tests.integration.test_bot_collaboration import setup

        engine = make_engine(migrated_database)
        try:
            async with make_session_factory(engine)() as session:
                a, b, _, route, _, row = await setup(session)
                return str(a.id), str(b.id), str(route.id), str(row.id)
        finally:
            await engine.dispose()

    cfg = alembic_config(migrated_database)
    aid, bid, rid, cid = asyncio.run(seed())
    try:
        command.downgrade(cfg, "0026")
        asyncio.run(
            _execute(
                migrated_database,
                (
                    "UPDATE bot_collaboration_routes SET enabled=false, timeout_seconds=120 "
                    "WHERE id=CAST(:id AS uuid)",
                    {"id": rid},
                ),
                (
                    "INSERT INTO bot_collaboration_routes "
                    "(id,source_bot_id,target_bot_id,chat_id,tenant_key,source_open_id,target_open_id,source_union_id,target_union_id,enabled,timeout_seconds,setup)"
                    " "
                    "SELECT "
                    "gen_random_uuid(),source_bot_id,target_bot_id,'second-group',tenant_key,source_open_id,target_open_id,source_union_id,target_union_id,true,60,'{\"status\":\"ready\"}'::jsonb"
                    " FROM bot_collaboration_routes WHERE id=CAST(:id AS uuid)",
                    {"id": rid},
                ),
                (
                    "INSERT INTO bot_collaboration_routes "
                    "(id,source_bot_id,target_bot_id,chat_id,tenant_key,source_open_id,target_open_id,source_union_id,target_union_id,enabled,archived)"
                    " "
                    "SELECT "
                    "gen_random_uuid(),target_bot_id,source_bot_id,'archived-group',tenant_key,target_open_id,source_open_id,target_union_id,source_union_id,true,true"
                    " FROM bot_collaboration_routes WHERE id=CAST(:id AS uuid)",
                    {"id": rid},
                ),
            )
        )
        command.upgrade(cfg, "head")
        assert asyncio.run(
            _rows(
                migrated_database,
                f"SELECT enabled,archived,timeout_seconds FROM bot_collaboration_partners "
                f"WHERE source_bot_id='{aid}'",
            )
        ) == [(True, False, 60)]
        assert asyncio.run(
            _rows(
                migrated_database,
                f"SELECT enabled,archived FROM bot_collaboration_partners WHERE "
                f"source_bot_id='{bid}'",
            )
        ) == [(False, True)]
        assert asyncio.run(
            _rows(
                migrated_database, f"SELECT route_id::text FROM bot_collaborations WHERE id='{cid}'"
            )
        ) == [(rid,)]
        assert asyncio.run(
            _rows(
                migrated_database,
                "SELECT count(DISTINCT setup->>'partner_id') FROM bot_collaboration_routes",
            )
        ) == [(2,)]
        assert asyncio.run(
            _rows(
                migrated_database,
                "SELECT setup->>'status' FROM bot_collaboration_routes WHERE "
                "chat_id='second-group'",
            )
        ) == [("ready",)]
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(
            _execute(
                migrated_database,
                (f"TRUNCATE {', '.join(BUSINESS_TABLES)} RESTART IDENTITY CASCADE", {}),
            )
        )
        asyncio.run(_restore_seed(migrated_database))
