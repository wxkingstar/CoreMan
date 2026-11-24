"""Allow an alert source to log routine recoveries without starting a task."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "bot_alert_sources",
        sa.Column(
            "skip_routine_recoveries", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("bot_alert_sources", "skip_routine_recoveries")
