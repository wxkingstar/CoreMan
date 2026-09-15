"""Opt-in Feishu bot collaboration ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_collaboration_routes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "target_bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        *[
            sa.Column(n, sa.Text(), nullable=False)
            for n in (
                "chat_id",
                "tenant_key",
                "source_open_id",
                "target_open_id",
                "source_union_id",
                "target_union_id",
            )
        ],
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default=sa.text("300"), nullable=False),
        sa.UniqueConstraint("source_bot_id", "target_bot_id", "chat_id"),
    )
    op.create_table(
        "bot_collaborations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "route_id",
            sa.Uuid(),
            sa.ForeignKey("bot_collaboration_routes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_task_id",
            sa.BigInteger(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("origin_user_id", sa.Uuid(), nullable=False),
        sa.Column("origin_platform_user_id", sa.Text(), nullable=False),
        sa.Column("source_session_key", sa.Text(), nullable=False),
        sa.Column("source_relay_session_id", sa.Uuid(), nullable=False),
        sa.Column("source_relay_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'requested'"), nullable=False),
        sa.Column(
            "request_outbox_id", sa.BigInteger(), sa.ForeignKey("outbox.id", ondelete="SET NULL")
        ),
        sa.Column(
            "response_outbox_id", sa.BigInteger(), sa.ForeignKey("outbox.id", ondelete="SET NULL")
        ),
        sa.Column("helper_task_id", sa.BigInteger()),
        sa.Column("resume_task_id", sa.BigInteger()),
        sa.Column("response", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bot_collaborations_status", "bot_collaborations", ["status"])
    op.create_index("ix_bot_collaborations_expires_at", "bot_collaborations", ["expires_at"])


def downgrade():
    op.drop_table("bot_collaborations")
    op.drop_table("bot_collaboration_routes")
