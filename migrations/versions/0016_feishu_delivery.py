"""Persist CardKit identity and monotonic sequence across gateway takeover."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feishu_deliveries",
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("task_streams.task_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("card_id", sa.Text()),
        sa.Column("message_id", sa.Text()),
        sa.Column("sequence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_static", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("failures", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("fallback", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("feishu_deliveries")
