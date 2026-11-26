"""Silent business-system tasks: `api_clients.silent_writes`, `api_tasks.silent` / `allowed_writes`.

A caller's silent writes are the operations (`<system>.<operationId>`) the platform admin lets it
run without a confirmation card. A task records whether it ran silently and which writes the
caller allowed (NULL = no restriction). Additive only: the previous version never reads them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "api_clients",
        sa.Column(
            "silent_writes",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "api_tasks",
        sa.Column("silent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("api_tasks", sa.Column("allowed_writes", postgresql.ARRAY(sa.Text())))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("api_tasks", "allowed_writes")
    op.drop_column("api_tasks", "silent")
    op.drop_column("api_clients", "silent_writes")
