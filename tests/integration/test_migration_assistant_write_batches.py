"""迁移 0109：审批单多一列 batch_id（合单），交互状态多一种 assistant_write；降级去掉两者。"""

from __future__ import annotations

import asyncio

from alembic import command

from tests.conftest import alembic_config
from tests.integration.test_migration_native_model_names import _rows

KINDS = (
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
    " WHERE conname LIKE '%kind' AND conrelid = 'interaction_states'::regclass"
)
COLUMN = (
    "SELECT column_name FROM information_schema.columns"
    " WHERE table_name = 'assistant_approvals' AND column_name = 'batch_id'"
)
INDEX = "SELECT indexname FROM pg_indexes WHERE indexname = 'assistant_approvals_batch_idx'"


def test_0109_adds_batches_and_the_card_kind_and_takes_them_back(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    try:
        assert "assistant_write" in asyncio.run(_rows(migrated_database, KINDS))[0][0]
        assert asyncio.run(_rows(migrated_database, COLUMN)) == [("batch_id",)]
        assert asyncio.run(_rows(migrated_database, INDEX)) == [("assistant_approvals_batch_idx",)]
        command.downgrade(cfg, "0108")
        assert "assistant_write" not in asyncio.run(_rows(migrated_database, KINDS))[0][0]
        assert "cloud_write" in asyncio.run(_rows(migrated_database, KINDS))[0][0]
        assert asyncio.run(_rows(migrated_database, COLUMN)) == []
        assert asyncio.run(_rows(migrated_database, INDEX)) == []
        command.upgrade(cfg, "head")
        assert asyncio.run(_rows(migrated_database, COLUMN)) == [("batch_id",)]
    finally:
        command.upgrade(cfg, "head")
