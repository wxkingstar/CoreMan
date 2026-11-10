"""task_streams.plan: the agent's own task list for the turn, shown as a checklist."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_streams",
        sa.Column(
            "plan",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("task_streams", "plan")
