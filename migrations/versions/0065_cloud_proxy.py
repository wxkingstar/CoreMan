"""Cloud signing proxy: call records, pending approvals and per-bot approvers.

Additive only: three new tables that the previous version never reads or writes.
"""

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cloud_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "called_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("task_id", sa.BigInteger()),
        sa.Column("bot_id", sa.Uuid()),
        sa.Column("user_id", sa.Uuid()),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("risk", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.Text()),
        sa.Column("request_hash", sa.Text()),
        sa.Column("status_code", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.CheckConstraint("risk IN ('read', 'write_low', 'write_high')", name="risk"),
        sa.CheckConstraint(
            "outcome IN ('ok', 'http_error', 'denied', 'approval_required', 'unreachable')",
            name="outcome",
        ),
    )
    op.create_index("cloud_calls_task_idx", "cloud_calls", ["task_id"])
    op.create_index("cloud_calls_time_idx", "cloud_calls", ["called_at"])
    op.create_table(
        "cloud_pending_approvals",
        sa.Column("external_id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.BigInteger()),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid()),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("origin_event_id", sa.BigInteger()),
        sa.Column("origin_relay_session_id", sa.Text()),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("approval_id", sa.Text()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.Text()),
        sa.Column("resume_task_id", sa.BigInteger()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'executed', 'withdrawn')",
            name="status",
        ),
    )
    op.create_table(
        "bot_approvers",
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_bot_approvers_bot_id_bots"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_bot_approvers_user_id_users"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("bot_id", "user_id", name="pk_bot_approvers"),
    )


def downgrade() -> None:
    op.drop_table("bot_approvers")
    op.drop_table("cloud_pending_approvals")
    op.drop_table("cloud_calls")
