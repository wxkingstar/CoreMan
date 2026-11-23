"""Claude background sessions: `background_runs`, node `live_sessions`, slotless runtime calls."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "background_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("relay_session_id", sa.Uuid(), nullable=False),
        sa.Column("origin_task_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_platform_user_id", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("node_id", sa.Uuid(), nullable=True),
        sa.Column("relay_server_id", sa.Uuid(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("chat_type", sa.Text(), nullable=False),
        sa.Column("origin_message_id", sa.Text(), nullable=True),
        sa.Column(
            "tasks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("close_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "card_ref", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("secrets_enc", sa.Text(), nullable=True),
        sa.Column("turns", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("card_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('running','finished','stopped','expired','lost')", name="state"
        ),
    )
    op.create_index(
        "background_runs_running_uq",
        "background_runs",
        ["bot_id", "session_key"],
        unique=True,
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_index(
        "background_runs_open_idx",
        "background_runs",
        ["deadline"],
        postgresql_where=sa.text("state = 'running'"),
    )
    op.add_column("runtime_nodes", sa.Column("live_sessions", postgresql.JSONB(), nullable=True))
    op.add_column(
        "runtime_calls",
        sa.Column("slotless", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("runtime_calls", "slotless")
    op.drop_column("runtime_nodes", "live_sessions")
    op.drop_index("background_runs_open_idx", table_name="background_runs")
    op.drop_index("background_runs_running_uq", table_name="background_runs")
    op.drop_table("background_runs")
