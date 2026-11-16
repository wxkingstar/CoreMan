"""Batch writes through the platform proxy: one confirmation for up to 1000 items of one operation.

The batch itself is an interaction state (kind `systems_batch`); its items are one row each in
`system_batch_items`, sealed, with their own idempotency key and outcome. Additive only: the
previous version never reads either during a rolling upgrade.
"""

import sqlalchemy as sa
from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None

KINDS_BEFORE = (
    "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
    " 'personal_schedule', 'feishu_group_write', 'systems_write', 'cloud_write',"
    " 'assistant_write')"
)
KINDS_AFTER = (
    "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
    " 'personal_schedule', 'feishu_group_write', 'systems_write', 'cloud_write',"
    " 'assistant_write', 'systems_batch')"
)
ITEM_STATUSES = (
    "status IN ('pending', 'sending', 'ok', 'http_error', 'unreachable', 'unknown', 'skipped')"
)


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint("kind", "interaction_states", KINDS_AFTER)
    op.create_table(
        "system_batch_items",
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey("interaction_states.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("request_enc", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("brief_enc", sa.Text()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(ITEM_STATUSES, name="status"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("system_batch_items")
    op.execute("DELETE FROM interaction_states WHERE kind = 'systems_batch'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint("kind", "interaction_states", KINDS_BEFORE)
