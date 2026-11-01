"""Runtime instances keep the last known quota of each seat that is not in use."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("relay_servers", sa.Column("seat_quotas", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("relay_servers", "seat_quotas")
