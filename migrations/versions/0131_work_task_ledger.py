"""Work tasks, phase 3: mirrored ledger rows (API tasks, help requests) and due reminders."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("work_tasks", sa.Column("origin_ref", sa.Text(), nullable=True))
    op.add_column(
        "work_tasks", sa.Column("due_reminded_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "work_tasks", sa.Column("overdue_notified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "work_tasks_origin_ref_idx",
        "work_tasks",
        ["origin_ref"],
        unique=True,
        postgresql_where=sa.text("origin_ref IS NOT NULL"),
    )
    op.create_index(
        "work_tasks_due_idx",
        "work_tasks",
        ["due_at"],
        postgresql_where=sa.text("due_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("work_tasks_due_idx", table_name="work_tasks")
    op.drop_index("work_tasks_origin_ref_idx", table_name="work_tasks")
    op.drop_column("work_tasks", "overdue_notified_at")
    op.drop_column("work_tasks", "due_reminded_at")
    op.drop_column("work_tasks", "origin_ref")
