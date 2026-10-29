"""Runtime nodes remember the source IP of their latest enrollment or heartbeat."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runtime_nodes", sa.Column("ip", postgresql.INET(), nullable=True))


def downgrade() -> None:
    op.drop_column("runtime_nodes", "ip")
