"""迁移 0094：已开通的助理从最近一条完成的开通流程回填身份；没有流程可依的不建行。"""

from __future__ import annotations

import asyncio

from alembic import command

from tests.conftest import alembic_config
from tests.integration.test_migration_native_model_names import _execute, _rows

USERS = (
    "INSERT INTO users (login_name, display_name) VALUES"
    " ('mig-zhang', '张三'), ('mig-li', '李四'), ('mig-wang', '王五')",
    {},
)
APP = (
    "INSERT INTO platform_apps (platform, name, capabilities, app_id, secret_enc)"
    " VALUES ('feishu', 'mig-assistant-app', ARRAY['assistant'], 'cli_example', 'x')",
    {},
)
BOT = (
    "INSERT INTO bots (bot_key, platform, name, created_by, owner_id, platform_app_id, kind,"
    " assistant_state, model, working_dir, credentials_enc)"
    " SELECT :key, 'feishu', '个人助理', u.id, u.id, a.id, 'assistant', 'active', 'm', '/w', ''"
    " FROM users u, platform_apps a WHERE u.login_name = :login AND a.name = 'mig-assistant-app'"
)
FLOW = (
    "INSERT INTO assistant_lifecycles (id, owner_id, platform_app_id, bot_id, action, step,"
    " outcome, platform_user_id, open_id, tenant_key, offer_chat_id, finished_at)"
    " SELECT gen_random_uuid(), b.owner_id, b.platform_app_id, b.id, 'provision', 'await_grant',"
    " :outcome, :pid, :open_id, 'tenant-example', :chat,"
    " now() - make_interval(hours => CAST(:age AS integer))"
    " FROM bots b WHERE b.bot_key = :key"
)
SETTINGS = (
    "SELECT b.bot_key, s.open_id, s.tenant_key, s.platform_user_id, s.chat_id, s.timezone,"
    " s.work_start, s.work_end, s.workdays"
    " FROM assistant_settings s JOIN bots b ON b.id = s.bot_id ORDER BY b.bot_key"
)


def _flow(key: str, outcome: str, pid: str, open_id: str, chat: str, age: int) -> tuple:
    return FLOW, {
        "key": key,
        "outcome": outcome,
        "pid": pid,
        "open_id": open_id,
        "chat": chat,
        "age": age,
    }


def test_0094_backfills_identity_from_the_latest_finished_provisioning(
    migrated_database: str,
) -> None:
    cfg = alembic_config(migrated_database)
    command.downgrade(cfg, "0093")
    try:
        asyncio.run(
            _execute(
                migrated_database,
                USERS,
                APP,
                (BOT, {"key": "mig-a", "login": "mig-zhang"}),
                (BOT, {"key": "mig-b", "login": "mig-li"}),
                (BOT, {"key": "mig-c", "login": "mig-wang"}),
                # 张三：两条完成的开通流程，取最近结束的那条。
                _flow("mig-a", "done", "zhangsan", "ou_old", "oc_old", 5),
                _flow("mig-a", "done", "zhangsan", "ou_zhang", "oc_zhang", 1),
                # 李四：只有一条失败的流程，不回填。
                _flow("mig-b", "failed", "lisi", "ou_li", "oc_li", 1),
                # 王五：本功能上线前建的记录，没有开通流程。
            )
        )
        command.upgrade(cfg, "0094")
        rows = asyncio.run(_rows(migrated_database, SETTINGS))
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(
            _execute(migrated_database, ("TRUNCATE bots, users, platform_apps CASCADE", {}))
        )
    assert rows == [
        (
            "mig-a",
            "ou_zhang",
            "tenant-example",
            "zhangsan",
            "oc_zhang",
            "Asia/Shanghai",
            "09:30",
            "19:00",
            [1, 2, 3, 4, 5],
        )
    ]
