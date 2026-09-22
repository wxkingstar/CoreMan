"""Outbound proxy the CLIs inherit, reported by runtime nodes in their heartbeat."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runtime_nodes", sa.Column("proxy", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("runtime_nodes", "proxy")
