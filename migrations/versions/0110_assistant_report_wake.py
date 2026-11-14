"""A personal assistant can mark a report as worth interrupting the owner's quiet hours
(assistant_reports.wake); only those are sent while quiet hours last."""

import sqlalchemy as sa
from alembic import op

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "assistant_reports",
        sa.Column("wake", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("assistant_reports", "wake")
