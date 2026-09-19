"""Add the max reasoning effort and set per-model xhigh/max support from the vendors' docs.

- bots.effort_level 允许 max；model_catalog 新增 supports_max。
- 种子与节点心跳写入的目录行从没设过 supports_xhigh，Opus 5、Sonnet 5、GPT-5.x 这些支持
  xhigh 的模型在页面上都选不了。按官方文档（2026-09-19 核对，出处见
  `coreman.core.relay.models.EXTRA_EFFORTS`）改正已知模型的两个标记；表里没有的模型保持
  管理员的设置（supports_max 为新列，一律从 false 起）。
- 某模型被改成不支持 xhigh 时，用它且档位为 xhigh 的机器人降到 high 并通知重载。Claude Code
  对不支持的档位本来就按 high 跑，这一步只是让库里的配置与实际一致。
"""

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

# 迁移写成时的快照，之后改 EXTRA_EFFORTS 不影响这里。(supports_xhigh, supports_max)
KNOWN = {
    "claude-fable-5-1": (True, True),
    "claude-fable-5": (True, True),
    "claude-mythos-5-1": (True, True),
    "claude-mythos-5": (True, True),
    "claude-mythos-preview": (False, True),
    "claude-opus-5": (True, True),
    "claude-opus-4-8": (True, True),
    "claude-opus-4-7": (True, True),
    "claude-opus-4-6": (False, True),
    "claude-opus-4-5": (False, False),
    "claude-sonnet-5": (True, True),
    "claude-sonnet-4-6": (False, True),
    "claude-sonnet-4-5": (False, False),
    "claude-haiku-4-5": (False, False),
    "gpt-6-astra": (True, True),
    "gpt-5.6-sol": (True, True),
    "gpt-5.6-terra": (True, True),
    "gpt-5.6-luna": (True, True),
    "gpt-5.5": (True, False),
    "gpt-5.4": (True, False),
    "gpt-5.4-mini": (True, False),
    "gpt-5.3-codex": (True, False),
    "gpt-5.2": (True, False),
}
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def known(model: str) -> tuple[bool, bool] | None:
    return KNOWN.get(_SNAPSHOT_SUFFIX.sub("", model.rsplit("/", 1)[-1]))


def _notify_bots(connection: sa.Connection, bot_ids: list[object]) -> None:
    for bot_id in bot_ids:
        connection.execute(
            sa.text("SELECT pg_notify('config_changed', :payload)"),
            {"payload": json.dumps({"table": "bots", "id": str(bot_id)})},
        )


def upgrade() -> None:
    op.add_column(
        "model_catalog",
        sa.Column("supports_max", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.drop_constraint(op.f("ck_bots_effort_level"), "bots", type_="check")
    op.create_check_constraint(
        "effort_level", "bots", "effort_level IN ('low','medium','high','xhigh','max')"
    )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT provider, model, supports_xhigh, supports_max FROM model_catalog")
    ).all()
    xhigh_off: set[str] = set()
    for provider, model, current_xhigh, current_max in rows:
        flags = known(model)
        if flags is None or flags == (current_xhigh, current_max):
            continue
        xhigh, max_ = flags
        connection.execute(
            sa.text(
                "UPDATE model_catalog SET supports_xhigh = :xhigh, supports_max = :max "
                "WHERE provider = :provider AND model = :model"
            ),
            {"xhigh": xhigh, "max": max_, "provider": provider, "model": model},
        )
        if current_xhigh and not xhigh:
            xhigh_off.add(model)
    if not xhigh_off:
        return
    # 同名模型可能挂在多个 provider 下，任一行仍支持就不降档（与 supports_effort() 同口径）。
    downgraded = (
        connection.execute(
            sa.text(
                "UPDATE bots SET effort_level = 'high', version = version + 1 "
                "WHERE effort_level = 'xhigh' AND model = ANY(:models) AND NOT EXISTS ("
                "SELECT 1 FROM model_catalog c WHERE c.model = bots.model AND c.supports_xhigh) "
                "RETURNING id"
            ),
            {"models": sorted(xhigh_off)},
        )
        .scalars()
        .all()
    )
    _notify_bots(connection, list(downgraded))


def downgrade() -> None:
    connection = op.get_bind()
    # 旧版本没有 max：机器人退到模型支持的 xhigh，否则 high；平台默认档位退到 xhigh。
    downgraded = (
        connection.execute(
            sa.text(
                "UPDATE bots SET version = version + 1, effort_level = CASE WHEN EXISTS ("
                "SELECT 1 FROM model_catalog c WHERE c.model = bots.model AND c.supports_xhigh) "
                "THEN 'xhigh' ELSE 'high' END WHERE effort_level = 'max' RETURNING id"
            )
        )
        .scalars()
        .all()
    )
    _notify_bots(connection, list(downgraded))
    connection.execute(
        sa.text(
            "UPDATE settings SET value = '\"xhigh\"'::jsonb "
            "WHERE key = 'default_effort_level' AND value = '\"max\"'::jsonb"
        )
    )
    op.drop_constraint(op.f("ck_bots_effort_level"), "bots", type_="check")
    op.create_check_constraint(
        "effort_level", "bots", "effort_level IN ('low','medium','high','xhigh')"
    )
    # supports_xhigh 的修正是纯数据改正，回退后保留。
    op.drop_column("model_catalog", "supports_max")
