"""Rename the seeded vllm/claude-* models to native Claude Code model names.

`vllm/` 前缀没有功能含义：驱动调用 CLI 前会去掉最后一个 `/` 之前的部分，两种写法到 CLI
都是同一个模型。驱动现在上报原生名，旧种子不改名的话目录里会出现两套同义条目。

逐个处理 0003 种子的三个模型：目录里没有原生名就原地改名；节点心跳已写入原生名时删掉
`vllm/` 行，把默认标记与显示名并到原生行。价格与机器人引用同步改名，机器人发
config_changed 让网关/工作进程重载。滚动升级窗口内旧版本进程读到任一写法都能正常调用。
聊天记录里的历史模型名保持原样。
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

PROVIDER = "claude"
RENAMES = (
    ("vllm/claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("vllm/claude-opus-4-6", "claude-opus-4-6"),
    ("vllm/claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
)


def upgrade() -> None:
    connection = op.get_bind()
    for old, new in RENAMES:
        params = {"provider": PROVIDER, "old": old, "new": new}
        legacy = (
            connection.execute(
                sa.text(
                    "SELECT is_default, retired, display_name FROM model_catalog "
                    "WHERE provider = :provider AND model = :old"
                ),
                params,
            )
            .mappings()
            .first()
        )
        if legacy is not None:
            native = connection.execute(
                sa.text("SELECT 1 FROM model_catalog WHERE provider = :provider AND model = :new"),
                params,
            ).first()
            if native is None:
                connection.execute(
                    sa.text(
                        "UPDATE model_catalog SET model = :new "
                        "WHERE provider = :provider AND model = :old"
                    ),
                    params,
                )
            else:
                connection.execute(
                    sa.text(
                        "DELETE FROM model_catalog WHERE provider = :provider AND model = :old"
                    ),
                    params,
                )
                # 两行任一未退役就保持可用；默认标记跟着旧种子走，显示名优先保留原生行已有的。
                connection.execute(
                    sa.text(
                        "UPDATE model_catalog SET is_default = is_default OR :is_default, "
                        "retired = retired AND :retired, "
                        "display_name = COALESCE(display_name, :display_name) "
                        "WHERE provider = :provider AND model = :new"
                    ),
                    {**params, **legacy},
                )
        connection.execute(
            sa.text(
                "UPDATE model_prices AS p SET model = :new "
                "WHERE p.provider = :provider AND p.model = :old AND NOT EXISTS ("
                "SELECT 1 FROM model_prices q WHERE q.provider = p.provider "
                "AND q.model = :new AND q.effective_from = p.effective_from)"
            ),
            params,
        )
        connection.execute(
            sa.text("DELETE FROM model_prices WHERE provider = :provider AND model = :old"),
            params,
        )
        renamed = connection.execute(
            sa.text(
                "UPDATE bots SET model = :new, version = version + 1 "
                "WHERE model = :old RETURNING id"
            ),
            params,
        ).scalars()
        for bot_id in renamed:
            connection.execute(
                sa.text("SELECT pg_notify('config_changed', :payload)"),
                {"payload": json.dumps({"table": "bots", "id": str(bot_id)})},
            )


def downgrade() -> None:
    # 纯数据改名，两种写法等价，回退无需改回。
    pass
