"""Durable runtime root change request and acknowledgement."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runtime_nodes", sa.Column("root_change", postgresql.JSONB(), nullable=True))
    op.add_column(
        "runtime_nodes",
        sa.Column("root_edit_supported", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("runtime_nodes", "root_edit_supported")
    op.drop_column("runtime_nodes", "root_change")
