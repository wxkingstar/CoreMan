"""Feishu group observing: per-bot switch (off / shadow / on) and scope, per-chat observe
state, and the decision log the admin console reviews before switching to on."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bots",
        sa.Column("group_observe", sa.Text(), nullable=False, server_default=sa.text("'off'")),
    )
    op.add_column(
        "bots",
        sa.Column("group_observe_scope", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.create_check_constraint("group_observe", "bots", "group_observe IN ('off','shadow','on')")
    op.create_table(
        "group_observe_states",
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("pending_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_id", sa.BigInteger(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bot_streak", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_interjected_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("bot_id", "chat_id"),
    )
    op.create_table(
        "group_observe_decisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("probability", sa.Float(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("review", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("mode IN ('shadow','on')", name="mode"),
        sa.CheckConstraint("review IS NULL OR review IN ('should','should_not')", name="review"),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "group_observe_decisions_bot_idx",
        "group_observe_decisions",
        ["bot_id", "created_at"],
    )
    op.create_index(
        "group_observe_decisions_created_idx", "group_observe_decisions", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("group_observe_decisions_created_idx", table_name="group_observe_decisions")
    op.drop_index("group_observe_decisions_bot_idx", table_name="group_observe_decisions")
    op.drop_table("group_observe_decisions")
    op.drop_table("group_observe_states")
    op.drop_constraint("group_observe", "bots", type_="check")
    op.drop_column("bots", "group_observe_scope")
    op.drop_column("bots", "group_observe")
