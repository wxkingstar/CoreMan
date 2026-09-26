"""Feishu AI employees may ask configured human colleagues for help."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_human_partners",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("responsibility", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("source_bot_id", "user_id"),
    )
    op.create_table(
        "human_collaborations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "partner_id",
            sa.Uuid(),
            sa.ForeignKey("bot_human_partners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "helper_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("helper_platform_user_id", sa.Text(), nullable=False),
        sa.Column(
            "source_task_id",
            sa.BigInteger(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("origin_kind", sa.Text(), nullable=False),
        sa.Column("origin_user_id", sa.Uuid(), nullable=False),
        sa.Column("origin_platform_user_id", sa.Text(), nullable=False),
        sa.Column("origin_chat_id", sa.Text()),
        sa.Column("origin_chat_type", sa.Text(), nullable=False),
        sa.Column("origin_event_id", sa.BigInteger()),
        sa.Column("relay_session_id", sa.Uuid()),
        sa.Column("cron_job_id", sa.Uuid()),
        sa.Column("cron_config", postgresql.JSONB()),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "request_outbox_id", sa.BigInteger(), sa.ForeignKey("outbox.id", ondelete="SET NULL")
        ),
        sa.Column("request_message_id", sa.Text()),
        sa.Column("reminder_message_id", sa.Text()),
        sa.Column("reminded_at", sa.DateTime(timezone=True)),
        sa.Column("reply_event_id", sa.BigInteger()),
        sa.Column("response", sa.Text()),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("resume_task_id", sa.BigInteger()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','waiting','answered','resuming',"
            "'completed','failed','cancelled','timed_out')",
            name="status",
        ),
        sa.CheckConstraint("channel IN ('group','direct')", name="channel"),
        sa.CheckConstraint("origin_kind IN ('chat','cron')", name="origin_kind"),
    )
    op.create_index(
        "human_collaborations_active_idx",
        "human_collaborations",
        ["bot_id", "status"],
        postgresql_where=sa.text("status IN ('pending','waiting','answered','resuming')"),
    )
    op.create_index(
        "human_collaborations_helper_idx",
        "human_collaborations",
        ["helper_user_id"],
        postgresql_where=sa.text("status IN ('pending','waiting','answered','resuming')"),
    )
    op.create_index(
        "human_collaborations_message_idx",
        "human_collaborations",
        ["bot_id", "request_message_id"],
    )


def downgrade() -> None:
    op.drop_index("human_collaborations_message_idx", table_name="human_collaborations")
    op.drop_index("human_collaborations_helper_idx", table_name="human_collaborations")
    op.drop_index("human_collaborations_active_idx", table_name="human_collaborations")
    op.drop_table("human_collaborations")
    op.drop_table("bot_human_partners")
