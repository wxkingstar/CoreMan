"""迁移 0051、0055、0061：新增最新模型、退役被替代的模型，机器人与白名单跟着换。"""

from __future__ import annotations

import asyncio

from alembic import command

from tests.conftest import alembic_config
from tests.integration.test_migration_native_model_names import _execute, _restore_seed, _rows

INSERT_MODEL = (
    "INSERT INTO model_catalog (provider, model, display_name, is_default, retired, sort_order)"
    " VALUES (:provider, :model, :display_name, :is_default, :retired, :sort_order)"
)
INSERT_BOT = (
    "INSERT INTO bots (bot_key, platform, name, created_by, model, working_dir, effort_level,"
    " credentials_enc) SELECT :key, 'wecom', :key, id, :model, '/data/x', :effort, ''"
    " FROM users WHERE login_name = 'migration-owner'"
)
INSERT_RELAY = (
    "INSERT INTO relay_servers (name, model_provider, supported_models_mode, supported_models)"
    " VALUES (:name, :provider, :mode, :models)"
)
CLEANUP = ("TRUNCATE bots, users, relay_servers CASCADE", {})


def _model(
    provider: str,
    model: str,
    *,
    default: bool = False,
    retired: bool = False,
    sort_order: int = 0,
    display_name: str | None = None,
) -> tuple[str, dict[str, object]]:
    return (
        INSERT_MODEL,
        {
            "provider": provider,
            "model": model,
            "display_name": display_name,
            "is_default": default,
            "retired": retired,
            "sort_order": sort_order,
        },
    )


def test_0051_adds_new_defaults_and_moves_bots_off_replaced_models(
    migrated_database: str,
) -> None:
    cfg = alembic_config(migrated_database)
    command.downgrade(cfg, "0050")
    try:
        asyncio.run(
            _execute(
                migrated_database,
                ("TRUNCATE model_catalog", {}),
                _model("claude", "claude-sonnet-5", default=True, sort_order=100),
                _model("claude", "claude-opus-5", sort_order=90),
                # 节点心跳写入过、又被管理员退役的新模型：取消退役，保留已有显示名。
                _model("claude", "claude-opus-5-5", retired=True, display_name="Opus 5.5 (node)"),
                _model("codex", "codex/gpt-6-astra", default=True, sort_order=100),
                _model("codex", "codex/gpt-5.6-sol", sort_order=90),
                _model("codex", "codex/gpt-5.6-luna", sort_order=70),
                # 快照日期写法同样算被替代；别的模型不动。
                _model("codex", "codex/gpt-5.6-terra-2026-05-01"),
                _model("codex", "codex/gpt-5.5"),
                (
                    "INSERT INTO users (login_name, display_name) "
                    "VALUES ('migration-owner', 'Owner')",
                    {},
                ),
                (INSERT_BOT, {"key": "opus", "model": "claude-opus-5", "effort": "max"}),
                (INSERT_BOT, {"key": "sonnet", "model": "claude-sonnet-5", "effort": None}),
                (
                    INSERT_BOT,
                    {"key": "terra", "model": "codex/gpt-5.6-terra-2026-05-01", "effort": "xhigh"},
                ),
                (INSERT_BOT, {"key": "astra", "model": "codex/gpt-6-astra", "effort": None}),
                (
                    INSERT_RELAY,
                    {
                        "name": "codex-restricted",
                        "provider": "codex",
                        "mode": "restricted",
                        "models": ["codex/gpt-6-astra", "codex/gpt-5.6-sol", "codex/gpt-5.6-luna"],
                    },
                ),
                (
                    INSERT_RELAY,
                    {
                        "name": "claude-restricted",
                        "provider": "claude",
                        "mode": "restricted",
                        "models": ["claude-sonnet-5"],
                    },
                ),
            )
        )
        command.upgrade(cfg, "0051")
        catalog = asyncio.run(
            _rows(
                migrated_database,
                "SELECT provider, model, display_name, is_default, retired, supports_xhigh,"
                " supports_max, sort_order FROM model_catalog ORDER BY 1, 2",
            )
        )
        bots = asyncio.run(
            _rows(
                migrated_database,
                "SELECT bot_key, model, effort_level, version FROM bots ORDER BY 1",
            )
        )
        relays = asyncio.run(
            _rows(
                migrated_database,
                "SELECT name, supported_models, version FROM relay_servers ORDER BY 1",
            )
        )
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_execute(migrated_database, CLEANUP))
        asyncio.run(_restore_seed(migrated_database))

    assert catalog == [
        ("claude", "claude-opus-5", None, False, True, False, False, 90),
        ("claude", "claude-opus-5-5", "Opus 5.5 (node)", True, False, True, True, 110),
        ("claude", "claude-sonnet-5", None, False, False, False, False, 100),
        ("codex", "codex/gpt-5.5", None, False, False, False, False, 0),
        ("codex", "codex/gpt-5.6-luna", None, False, True, False, False, 70),
        ("codex", "codex/gpt-5.6-sol", None, False, True, False, False, 90),
        ("codex", "codex/gpt-5.6-terra-2026-05-01", None, False, True, False, False, 0),
        ("codex", "codex/gpt-6-astra", None, False, False, False, False, 100),
        ("codex", "codex/gpt-6-sol", "GPT-6 Sol (Codex)", True, False, True, True, 110),
    ]
    # 新模型支持全部档位，effort_level 原样保留。
    assert bots == [
        ("astra", "codex/gpt-6-astra", None, 1),
        ("opus", "claude-opus-5-5", "max", 2),
        ("sonnet", "claude-sonnet-5", None, 1),
        ("terra", "codex/gpt-6-sol", "xhigh", 2),
    ]
    # 新模型顶替第一个退役模型的位置，重复的退役项去掉；不含退役模型的白名单不动。
    assert relays == [
        ("claude-restricted", ["claude-sonnet-5"], 1),
        ("codex-restricted", ["codex/gpt-6-astra", "codex/gpt-6-sol"], 2),
    ]


def test_0055_replaces_gpt_6_sol_with_gpt_6_1_sol(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    command.downgrade(cfg, "0054")
    try:
        asyncio.run(
            _execute(
                migrated_database,
                ("TRUNCATE model_catalog", {}),
                _model("claude", "claude-opus-5-5", default=True, sort_order=110),
                _model("codex", "codex/gpt-6-sol", default=True, sort_order=110),
                _model("codex", "codex/gpt-6-astra", sort_order=100),
                # 快照日期写法同样算被替代。
                _model("codex", "codex/gpt-6-sol-2026-09-01"),
                (
                    "INSERT INTO users (login_name, display_name) "
                    "VALUES ('migration-owner', 'Owner')",
                    {},
                ),
                (INSERT_BOT, {"key": "sol", "model": "codex/gpt-6-sol", "effort": "max"}),
                (INSERT_BOT, {"key": "astra", "model": "codex/gpt-6-astra", "effort": None}),
                (
                    INSERT_RELAY,
                    {
                        "name": "codex-restricted",
                        "provider": "codex",
                        "mode": "restricted",
                        "models": ["codex/gpt-6-sol", "codex/gpt-6-astra"],
                    },
                ),
            )
        )
        command.upgrade(cfg, "0055")
        catalog = asyncio.run(
            _rows(
                migrated_database,
                "SELECT provider, model, display_name, is_default, retired, supports_xhigh,"
                ' supports_max, sort_order FROM model_catalog ORDER BY 1, model COLLATE "C"',
            )
        )
        bots = asyncio.run(
            _rows(
                migrated_database,
                "SELECT bot_key, model, effort_level, version FROM bots ORDER BY 1",
            )
        )
        relays = asyncio.run(
            _rows(
                migrated_database,
                "SELECT name, supported_models, version FROM relay_servers ORDER BY 1",
            )
        )
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_execute(migrated_database, CLEANUP))
        asyncio.run(_restore_seed(migrated_database))

    assert catalog == [
        ("claude", "claude-opus-5-5", None, True, False, False, False, 110),
        ("codex", "codex/gpt-6-astra", None, False, False, False, False, 100),
        ("codex", "codex/gpt-6-sol", None, False, True, False, False, 110),
        ("codex", "codex/gpt-6-sol-2026-09-01", None, False, True, False, False, 0),
        ("codex", "codex/gpt-6.1-sol", "GPT-6.1 Sol (Codex)", True, False, True, True, 120),
    ]
    assert bots == [
        ("astra", "codex/gpt-6-astra", None, 1),
        ("sol", "codex/gpt-6.1-sol", "max", 2),
    ]
    assert relays == [("codex-restricted", ["codex/gpt-6.1-sol", "codex/gpt-6-astra"], 2)]


def test_0061_keeps_only_latest_claude_of_each_line(migrated_database: str) -> None:
    cfg = alembic_config(migrated_database)
    command.downgrade(cfg, "0060")
    try:
        asyncio.run(
            _execute(
                migrated_database,
                ("TRUNCATE model_catalog", {}),
                _model("claude", "claude-opus-5-5", default=True, sort_order=110),
                _model("claude", "claude-sonnet-5", sort_order=100),
                _model("claude", "claude-haiku-4-5-20251001", sort_order=80),
                _model("claude", "claude-fable-5-1", sort_order=70),
                _model("claude", "claude-opus-4-6"),
                _model("claude", "claude-sonnet-4-6"),
                _model("claude", "claude-mythos-5-1"),
                # 节点心跳写入过、又被管理员退役的新模型：取消退役，保留已有显示名。
                _model("claude", "claude-haiku-5-5", retired=True, display_name="Haiku (node)"),
                _model("codex", "codex/gpt-6.1-sol", default=True, sort_order=120),
                (
                    "INSERT INTO users (login_name, display_name) "
                    "VALUES ('migration-owner', 'Owner')",
                    {},
                ),
                (INSERT_BOT, {"key": "opus46", "model": "claude-opus-4-6", "effort": "max"}),
                (INSERT_BOT, {"key": "sonnet5", "model": "claude-sonnet-5", "effort": "xhigh"}),
                (
                    INSERT_BOT,
                    {"key": "haiku", "model": "claude-haiku-4-5-20251001", "effort": None},
                ),
                (INSERT_BOT, {"key": "fable", "model": "claude-fable-5-1", "effort": None}),
                (
                    INSERT_RELAY,
                    {
                        "name": "claude-restricted",
                        "provider": "claude",
                        "mode": "restricted",
                        "models": ["claude-sonnet-4-6", "claude-sonnet-5", "claude-fable-5-1"],
                    },
                ),
            )
        )
        command.upgrade(cfg, "0061")
        catalog = asyncio.run(
            _rows(
                migrated_database,
                "SELECT provider, model, display_name, is_default, retired, supports_xhigh,"
                ' supports_max, sort_order FROM model_catalog ORDER BY 1, model COLLATE "C"',
            )
        )
        bots = asyncio.run(
            _rows(
                migrated_database,
                "SELECT bot_key, model, effort_level, version FROM bots ORDER BY 1",
            )
        )
        relays = asyncio.run(
            _rows(
                migrated_database,
                "SELECT name, supported_models, version FROM relay_servers ORDER BY 1",
            )
        )
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_execute(migrated_database, CLEANUP))
        asyncio.run(_restore_seed(migrated_database))

    assert catalog == [
        ("claude", "claude-fable-5-1", None, False, False, False, False, 70),
        ("claude", "claude-haiku-4-5-20251001", None, False, True, False, False, 80),
        ("claude", "claude-haiku-5-5", "Haiku (node)", False, False, True, True, 80),
        ("claude", "claude-mythos-5-1", None, False, False, False, False, 0),
        ("claude", "claude-opus-4-6", None, False, True, False, False, 0),
        ("claude", "claude-opus-5-5", None, True, False, False, False, 110),
        ("claude", "claude-sonnet-4-6", None, False, True, False, False, 0),
        ("claude", "claude-sonnet-5", None, False, True, False, False, 100),
        ("claude", "claude-sonnet-5-5", "Claude Sonnet 5.5", False, False, True, True, 100),
        ("codex", "codex/gpt-6.1-sol", None, True, False, False, False, 120),
    ]
    # 机器人改到同系列最新模型，effort_level 原样保留；本就在最新模型上的不动。
    assert bots == [
        ("fable", "claude-fable-5-1", None, 1),
        ("haiku", "claude-haiku-5-5", None, 2),
        ("opus46", "claude-opus-5-5", "max", 2),
        ("sonnet5", "claude-sonnet-5-5", "xhigh", 2),
    ]
    # 同系列多个退役模型只留一个新模型，占第一个的位置。
    assert relays == [("claude-restricted", ["claude-sonnet-5-5", "claude-fable-5-1"], 2)]
