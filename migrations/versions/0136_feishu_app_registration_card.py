"""Scope top-ups started from a chat card: `feishu_app_registrations.notify_card`.

The scheduler polls these sessions for the admin and writes the outcome back to the card. Additive
only: the previous version never reads the new column during a rolling upgrade.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "feishu_app_registrations",
        sa.Column("notify_card", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("feishu_app_registrations", "notify_card")
