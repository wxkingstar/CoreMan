"""Work tasks: whether the dispatcher tracks a task in the group. Simple one-off requests are
still recorded, but the group sees no task code and the assignee just answers."""

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "work_tasks",
        sa.Column("tracked", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("work_tasks", "tracked")
