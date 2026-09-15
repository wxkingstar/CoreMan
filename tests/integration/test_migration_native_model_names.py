"""迁移 0023：`vllm/claude-*` 种子改成原生模型名，与节点心跳写入的同名行合并。"""

from __future__ import annotations

import asyncio
from datetime import date

from alembic import command
from sqlalchemy import text

from coreman.core.db.session import make_engine
from tests.conftest import SEED_MODEL_CATALOG, alembic_config, model_catalog_seed

DAY = date(2026, 1, 1)


async def _execute(url: str, *statements: tuple[str, dict[str, object]]) -> None:
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            for sql, params in statements:
                await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _rows(url: str, sql: str) -> list[tuple[object, ...]]:
    engine = make_engine(url)
    try:
        async with engine.connect() as conn:
            return [tuple(row) for row in (await conn.execute(text(sql))).all()]
    finally:
        await engine.dispose()


async def _restore_seed(url: str) -> None:
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE model_catalog, model_prices"))
            await conn.execute(SEED_MODEL_CATALOG, model_catalog_seed())
    finally:
        await engine.dispose()


def test_0023_renames_seeded_models_and_merges_heartbeat_rows(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    insert_model = (
        "INSERT INTO model_catalog (provider, model, display_name, is_default, retired, sort_order)"
        " VALUES ('claude', :model, :display_name, :is_default, :retired, :sort_order)"
    )
    insert_price = (
        "INSERT INTO model_prices (provider, model, effective_from, input_usd, output_usd,"
        " cache_read_usd, cache_write_usd) VALUES ('claude', :model, :day, :usd, 1, 0, 0)"
    )
    command.downgrade(cfg, "0022")
    try:
        asyncio.run(
            _execute(
                migrated_database,
                ("TRUNCATE model_catalog, model_prices", {}),
                # 只有旧种子：原地改名，默认标记跟着走。
                (
                    insert_model,
                    {
                        "model": "vllm/claude-sonnet-4-6",
                        "display_name": "Claude Sonnet 4.6",
                        "is_default": True,
                        "retired": False,
                        "sort_order": 100,
                    },
                ),
                # 旧种子与心跳写入的原生名并存：删旧行，原生行保留自己的显示名、恢复可用。
                (
                    insert_model,
                    {
                        "model": "vllm/claude-opus-4-6",
                        "display_name": "Claude Opus 4.6",
                        "is_default": False,
                        "retired": False,
                        "sort_order": 90,
                    },
                ),
                (
                    insert_model,
                    {
                        "model": "claude-opus-4-6",
                        "display_name": "Opus (node)",
                        "is_default": False,
                        "retired": True,
                        "sort_order": 0,
                    },
                ),
                # 别的前缀不属于这次改名。
                (
                    insert_model,
                    {
                        "model": "kimi/kimi-k2.5",
                        "display_name": "Kimi K2.5",
                        "is_default": False,
                        "retired": False,
                        "sort_order": 50,
                    },
                ),
                (insert_price, {"model": "vllm/claude-sonnet-4-6", "day": DAY, "usd": 3}),
                (insert_price, {"model": "vllm/claude-opus-4-6", "day": DAY, "usd": 15}),
                (insert_price, {"model": "claude-opus-4-6", "day": DAY, "usd": 16}),
            )
        )
        command.upgrade(cfg, "head")
        catalog = asyncio.run(
            _rows(
                migrated_database,
                "SELECT model, display_name, is_default, retired FROM model_catalog ORDER BY model",
            )
        )
        prices = asyncio.run(
            _rows(migrated_database, "SELECT model, input_usd FROM model_prices ORDER BY model")
        )
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_restore_seed(migrated_database))

    assert catalog == [
        ("claude-opus-4-6", "Opus (node)", False, False),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6", True, False),
        ("kimi/kimi-k2.5", "Kimi K2.5", False, False),
    ]
    # 原生名已有同日价格时保留原生行，旧行删除。
    assert [(model, float(str(usd))) for model, usd in prices] == [
        ("claude-opus-4-6", 16.0),
        ("claude-sonnet-4-6", 3.0),
    ]
