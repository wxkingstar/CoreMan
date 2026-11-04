"""Separate driver/model health and retain per-window quota provenance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("quota_observation", "driver_health", "model_health", "last_model_failure"):
        op.add_column("relay_servers", sa.Column(name, postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    for name in ("last_model_failure", "model_health", "driver_health", "quota_observation"):
        op.drop_column("relay_servers", name)
