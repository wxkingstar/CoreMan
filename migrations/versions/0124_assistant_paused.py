"""Owner pause switch for personal assistants: `assistant_settings.paused_at`."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "assistant_settings", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("assistant_settings", "paused_at")
