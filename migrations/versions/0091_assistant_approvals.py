"""Personal assistant approval gate: staged writes with their decisions, and processing marks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None

STATUSES = (
    "status IN ('pending', 'approved', 'rejected', 'withdrawn', 'stale', 'executed',"
    " 'failed', 'expired')"
)
CHANNELS = "channel IN ('p2p', 'group', 'doc', 'approval', 'calendar', 'task', 'mail', 'other')"


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "assistant_approvals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("args_enc", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("args_hash", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("anchor_message_id", sa.Text(), nullable=True),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("topic", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("reason_enc", sa.Text(), nullable=True),
        sa.Column("result_enc", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.Text(), nullable=True),
        sa.Column("origin_event_id", sa.BigInteger(), nullable=True),
        sa.Column("session_key", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("has_files", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        _ts("expires_at", nullable=False),
        _ts("notified_at", nullable=True),
        _ts("decided_at", nullable=True),
        _ts("execute_started_at", nullable=True),
        _ts("executed_at", nullable=True),
        sa.CheckConstraint(STATUSES, name="status"),
        sa.CheckConstraint(CHANNELS, name="channel"),
        sa.UniqueConstraint("external_id", name="uq_assistant_approvals_external_id"),
    )
    op.create_index(
        "assistant_approvals_bot_status_idx", "assistant_approvals", ["bot_id", "status"]
    )
    op.create_index(
        "assistant_approvals_live_uq",
        "assistant_approvals",
        ["bot_id", "args_hash"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'approved')"),
    )
    op.create_table(
        "assistant_marks",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("reaction_id", sa.Text(), nullable=False),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("bot_id", "message_id"),
    )


def downgrade() -> None:
    op.drop_table("assistant_marks")
    op.drop_index("assistant_approvals_live_uq", table_name="assistant_approvals")
    op.drop_index("assistant_approvals_bot_status_idx", table_name="assistant_approvals")
    op.drop_table("assistant_approvals")
