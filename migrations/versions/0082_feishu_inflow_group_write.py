"""Feishu in-flow authorization waits and group writes as the requester; kind feishu_group_write."""

import sqlalchemy as sa
from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feishu_auth_waits",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column(
            "origin_task_id",
            sa.BigInteger(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("need", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('offered', 'waiting', 'resumed', 'expired')", name="status"),
    )
    op.create_index(
        "feishu_auth_waits_live_uq",
        "feishu_auth_waits",
        ["bot_id", "user_id", "chat_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('offered', 'waiting')"),
    )
    op.create_table(
        "feishu_group_actions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("upstream_code", sa.BigInteger(), nullable=True),
        sa.Column("args_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "feishu_group_actions_task_idx",
        "feishu_group_actions",
        ["task_id", "tool", "args_hash"],
    )
    op.create_index(
        "feishu_group_actions_user_idx",
        "feishu_group_actions",
        ["bot_id", "user_id", "created_at"],
    )
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint(
        "kind",
        "interaction_states",
        "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
        " 'personal_schedule', 'feishu_group_write')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM interaction_states WHERE kind = 'feishu_group_write'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint(
        "kind",
        "interaction_states",
        "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
        " 'personal_schedule')",
    )
    op.drop_table("feishu_group_actions")
    op.drop_index("feishu_auth_waits_live_uq", table_name="feishu_auth_waits")
    op.drop_table("feishu_auth_waits")
