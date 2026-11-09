"""Alert sources: bot messages from a registered sender start a turn as a fixed service user."""

import sqlalchemy as sa
from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_alert_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("sender_union_id", sa.Text(), nullable=False),
        sa.Column(
            "service_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("dedupe_seconds", sa.Integer(), server_default=sa.text("600"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("bot_id", "chat_id", "sender_union_id", name="bot_alert_sources_key"),
    )
    op.create_table(
        "bot_alert_states",
        sa.Column(
            "source_id",
            sa.Uuid(),
            sa.ForeignKey("bot_alert_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alert_key", sa.Text(), nullable=False),
        sa.Column("last_task_id", sa.BigInteger(), nullable=True),
        sa.Column("last_event_id", sa.BigInteger(), nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("repeats", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("source_id", "alert_key"),
    )


def downgrade() -> None:
    op.drop_table("bot_alert_states")
    op.drop_table("bot_alert_sources")
