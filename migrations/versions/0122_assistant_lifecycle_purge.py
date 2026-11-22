"""Assistant lifecycles: `purge` marks a shutdown that deletes the records and all their data."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "assistant_lifecycles",
        sa.Column("purge", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("assistant_lifecycles", "purge")
