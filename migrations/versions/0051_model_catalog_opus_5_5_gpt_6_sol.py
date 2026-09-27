"""Add Claude Opus 5.5 and GPT-6 Sol as the defaults and retire the models they replace.

- 目录新增 claude-opus-5-5、codex/gpt-6-sol（已存在则取消退役），排到各自 provider 最前并设为默认。
  两者都支持 xhigh 与 max（2026-09-27 核对官方文档，出处见
  `coreman.core.relay.models.EXTRA_EFFORTS`）。
- 退役被替代的 Claude Opus 5 与 GPT-5.6 Sol / Terra / Luna。按去掉 `codex/` 等前缀与快照日期后的
  名字匹配，节点心跳写入的其他写法一起退役；GPT-6 Astra、Sonnet 5、Haiku 4.5、Fable 5.1 不动。
- 用退役模型的机器人改到同 provider 的新模型，发 config_changed 让网关/工作进程重载。两个新模型
  支持全部思考档位，effort_level 不用调整。
- 白名单（restricted）运行时里的退役模型换成新模型，否则迁过去的机器人不在其有效模型集内。

聊天记录与价格表里的历史模型名保持原样。
"""

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

# provider -> (新模型, 显示名, 被替代模型去掉前缀与快照日期后的名字)
REPLACEMENTS = {
    "claude": ("claude-opus-5-5", "Claude Opus 5.5", {"claude-opus-5"}),
    "codex": (
        "codex/gpt-6-sol",
        "GPT-6 Sol (Codex)",
        {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"},
    ),
}
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def base_name(model: str) -> str:
    return _SNAPSHOT_SUFFIX.sub("", model.rsplit("/", 1)[-1])


def upgrade() -> None:
    connection = op.get_bind()
    for provider, (new, display_name, replaced) in REPLACEMENTS.items():
        params = {"provider": provider, "new": new}
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
            {**params, "display_name": display_name, "sort_order": top + 10},
        )
        # 同一 provider 至多一个默认：先清再设。
        connection.execute(
            sa.text(
                "UPDATE model_catalog SET is_default = (model = :new) WHERE provider = :provider"
            ),
            params,
        )

        models = connection.execute(
            sa.text("SELECT model FROM model_catalog WHERE provider = :provider"), params
        ).scalars()
        old = sorted(m for m in models if base_name(m) in replaced)
        if not old:
            continue
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
            replaced_list = [new if m in old else m for m in supported]
            deduped = list(dict.fromkeys(replaced_list))
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
