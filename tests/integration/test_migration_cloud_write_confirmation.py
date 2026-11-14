"""迁移 0106：交互状态多一种 cloud_write，cloud_calls 多出暂存与确认后执行的结局；降级折回旧值。"""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError

from tests.conftest import alembic_config
from tests.integration.test_migration_native_model_names import _execute, _rows

INSERT_CALL = (
    "INSERT INTO cloud_calls (product, action, risk, outcome)"
    " VALUES ('slb', 'AddAccessControlListEntry', 'write_low', :outcome)"
)
OUTCOMES = "SELECT outcome FROM cloud_calls ORDER BY id"


def test_0106_adds_outcomes_and_folds_them_back(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    asyncio.run(_execute(migrated_database, ("DELETE FROM cloud_calls", {})))
    try:
        asyncio.run(
            _execute(
                migrated_database,
                *(
                    (INSERT_CALL, {"outcome": outcome})
                    for outcome in ("ok", "proposed", "confirmed_ok", "confirmed_denied")
                ),
            )
        )
        command.downgrade(cfg, "0105")
        assert asyncio.run(_rows(migrated_database, OUTCOMES)) == [
            ("ok",),
            ("approval_required",),
            ("ok",),
            ("denied",),
        ]
        with pytest.raises(IntegrityError):
            asyncio.run(_execute(migrated_database, (INSERT_CALL, {"outcome": "proposed"})))
        command.upgrade(cfg, "head")
        asyncio.run(
            _execute(migrated_database, (INSERT_CALL, {"outcome": "confirmed_unreachable"}))
        )
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_execute(migrated_database, ("DELETE FROM cloud_calls", {})))
