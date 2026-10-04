"""Add GPT-6.1 Sol as the Codex default and retire GPT-6 Sol.

- 目录新增 codex/gpt-6.1-sol（已存在则取消退役），排到 codex 最前并设为默认，支持 xhigh 与 max
  （同 GPT-6 Sol）。
- 退役 GPT-6 Sol。按去掉 `codex/` 等前缀与快照日期后的名字匹配，节点心跳写入的其他写法一起退役；
  其余模型不动。
- 用 GPT-6 Sol 的机器人改到 GPT-6.1 Sol，发 config_changed 让网关/工作进程重载；
  effort_level 不用调整。
- 白名单（restricted）运行时里的 GPT-6 Sol 换成 GPT-6.1 Sol。

聊天记录与价格表里的历史模型名保持原样。
"""

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

PROVIDER = "codex"
NEW = "codex/gpt-6.1-sol"
DISPLAY_NAME = "GPT-6.1 Sol (Codex)"
REPLACED = {"gpt-6-sol"}
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def base_name(model: str) -> str:
    return _SNAPSHOT_SUFFIX.sub("", model.rsplit("/", 1)[-1])


def upgrade() -> None:
    connection = op.get_bind()
    params = {"provider": PROVIDER, "new": NEW}
    top = connection.execute(
        sa.text(
            "SELECT COALESCE(MAX(sort_order), 0) FROM model_catalog "
            "WHERE provider = :provider AND model <> :new"
        ),
        params,
    ).scalar_one()
    connection.execute(
        sa.text(
            "INSERT INTO model_catalog (provider, model, display_name, retired, "
            "supports_xhigh, supports_max, sort_order) "
            "VALUES (:provider, :new, :display_name, false, true, true, :sort_order) "
            "ON CONFLICT (provider, model) DO UPDATE SET retired = false, "
            "supports_xhigh = true, supports_max = true, sort_order = :sort_order, "
            "display_name = COALESCE(model_catalog.display_name, :display_name)"
        ),
        {**params, "display_name": DISPLAY_NAME, "sort_order": top + 10},
    )
    # 同一 provider 至多一个默认：先清再设。
    connection.execute(
        sa.text("UPDATE model_catalog SET is_default = (model = :new) WHERE provider = :provider"),
        params,
    )

    models = connection.execute(
        sa.text("SELECT model FROM model_catalog WHERE provider = :provider"), params
    ).scalars()
    old = sorted(m for m in models if base_name(m) in REPLACED)
    if not old:
        return
    old_params = {**params, "old": old}
    connection.execute(
        sa.text(
            "UPDATE model_catalog SET retired = true, is_default = false "
            "WHERE provider = :provider AND model = ANY(:old)"
        ),
        old_params,
    )
    relays = connection.execute(
        sa.text(
            "SELECT id, supported_models FROM relay_servers "
            "WHERE model_provider = :provider AND supported_models_mode = 'restricted' "
            "AND supported_models && CAST(:old AS text[])"
        ),
        old_params,
    ).all()
    for relay_id, supported in relays:
        # 新模型顶替第一个退役模型的位置（白名单顺序即页面上的顺序），其余退役模型去掉。
        deduped = list(dict.fromkeys(NEW if m in old else m for m in supported))
        connection.execute(
            sa.text(
                "UPDATE relay_servers SET supported_models = :models, "
                "version = version + 1 WHERE id = :id"
            ),
            {"models": deduped, "id": relay_id},
        )
    moved = connection.execute(
        sa.text(
            "UPDATE bots SET model = :new, version = version + 1 "
            "WHERE model = ANY(:old) RETURNING id"
        ),
        old_params,
    ).scalars()
    for bot_id in moved:
        connection.execute(
            sa.text("SELECT pg_notify('config_changed', :payload)"),
            {"payload": json.dumps({"table": "bots", "id": str(bot_id)})},
        )


def downgrade() -> None:
    # 纯数据调整：目录与机器人的选择回退后保留，管理员可在模型目录里自行改回。
    pass
