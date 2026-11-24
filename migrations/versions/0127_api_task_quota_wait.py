"""API tasks waiting for runtime quota: `api_tasks.quota_waiting_since` and `resume_at`."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "api_tasks",
        sa.Column("quota_waiting_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("api_tasks", sa.Column("resume_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("api_tasks", "resume_at")
    op.drop_column("api_tasks", "quota_waiting_since")
