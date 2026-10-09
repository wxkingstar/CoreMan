"""Keep only the latest Claude model of each line: add Sonnet 5.5 and Haiku 5.5, retire the rest.

- 目录新增 claude-sonnet-5-5、claude-haiku-5-5（已存在则取消退役），两者都支持 xhigh 与 max
  （2026-10-09 核对官方文档，出处见 `coreman.core.relay.models.EXTRA_EFFORTS`）。
  Fable 5.1、Opus 5.5 已在目录里，默认模型不变。
- 各系列只留最新一个：退役 Fable 5，Opus 5 / 4.8 / 4.7 / 4.6 / 4.5，Sonnet 5 / 4.6 / 4.5，
  Haiku 4.5。按去掉前缀与快照日期后的名字匹配，节点心跳写入的其他写法一起退役；Mythos 不动。
- 用退役模型的机器人改到同系列的最新模型，发 config_changed 让网关/工作进程重载。新模型
  支持全部思考档位，effort_level 不用调整。
- 白名单（restricted）运行时里的退役模型换成同系列的最新模型。

聊天记录与价格表里的历史模型名保持原样。
"""

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

PROVIDER = "claude"
# 新模型 -> (显示名, 新增时的 sort_order, 被替代模型去掉前缀与快照日期后的名字)。
# sort_order 为 None 表示目录里已有、不新增。
LINES = {
    "claude-fable-5-1": ("Claude Fable 5.1", None, {"claude-fable-5"}),
    "claude-opus-5-5": (
        "Claude Opus 5.5",
        None,
        {
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-opus-4-5",
        },
    ),
    "claude-sonnet-5-5": (
        "Claude Sonnet 5.5",
        100,
        {"claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5"},
    ),
    "claude-haiku-5-5": ("Claude Haiku 5.5", 80, {"claude-haiku-4-5"}),
}
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def base_name(model: str) -> str:
    return _SNAPSHOT_SUFFIX.sub("", model.rsplit("/", 1)[-1])


def replacement_of(model: str) -> str | None:
    base = base_name(model)
    return next((new for new, (_, _, replaced) in LINES.items() if base in replaced), None)


def upgrade() -> None:
    connection = op.get_bind()
    for new, (display_name, sort_order, _) in LINES.items():
        if sort_order is None:
            continue
        connection.execute(
            sa.text(
                "INSERT INTO model_catalog (provider, model, display_name, retired, "
                "supports_xhigh, supports_max, sort_order) "
                "VALUES (:provider, :new, :display_name, false, true, true, :sort_order) "
                "ON CONFLICT (provider, model) DO UPDATE SET retired = false, "
                "supports_xhigh = true, supports_max = true, sort_order = :sort_order, "
                "display_name = COALESCE(model_catalog.display_name, :display_name)"
            ),
            {
                "provider": PROVIDER,
                "new": new,
                "display_name": display_name,
                "sort_order": sort_order,
            },
        )

    models = connection.execute(
        sa.text("SELECT model FROM model_catalog WHERE provider = :provider"),
        {"provider": PROVIDER},
    ).scalars()
    moves = {m: new for m in models if (new := replacement_of(m)) is not None}
    if not moves:
        return
    old = sorted(moves)
    connection.execute(
        sa.text(
            "UPDATE model_catalog SET retired = true, is_default = false "
            "WHERE provider = :provider AND model = ANY(:old)"
        ),
        {"provider": PROVIDER, "old": old},
    )
    relays = connection.execute(
        sa.text(
            "SELECT id, supported_models FROM relay_servers "
            "WHERE model_provider = :provider AND supported_models_mode = 'restricted' "
            "AND supported_models && CAST(:old AS text[])"
        ),
        {"provider": PROVIDER, "old": old},
    ).all()
    for relay_id, supported in relays:
        # 新模型顶替同系列第一个退役模型的位置（白名单顺序即页面上的顺序），重复项去掉。
        deduped = list(dict.fromkeys(moves.get(m, m) for m in supported))
        connection.execute(
            sa.text(
                "UPDATE relay_servers SET supported_models = :models, "
                "version = version + 1 WHERE id = :id"
            ),
            {"models": deduped, "id": relay_id},
        )
    for new in LINES:
        replaced = [m for m, target in moves.items() if target == new]
        if not replaced:
            continue
        moved = connection.execute(
            sa.text(
                "UPDATE bots SET model = :new, version = version + 1 "
                "WHERE model = ANY(:old) RETURNING id"
            ),
            {"new": new, "old": replaced},
        ).scalars()
        for bot_id in moved:
            connection.execute(
                sa.text("SELECT pg_notify('config_changed', :payload)"),
                {"payload": json.dumps({"table": "bots", "id": str(bot_id)})},
            )


def downgrade() -> None:
    # 纯数据调整：目录与机器人的选择回退后保留，管理员可在模型目录里自行改回。
    pass
