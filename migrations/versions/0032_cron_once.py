"""Real once schedules with transactional consumption, preserving recurring jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "cron_jobs",
        sa.Column("schedule_kind", sa.Text(), nullable=False, server_default="recurring"),
    )
    op.add_column("cron_jobs", sa.Column("run_at", sa.DateTime(timezone=True)))
    op.add_column("cron_jobs", sa.Column("consumed_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "schedule_kind", "cron_jobs", "schedule_kind IN ('recurring', 'once')"
    )
    op.create_check_constraint(
        "once_run_at", "cron_jobs", "schedule_kind != 'once' OR run_at IS NOT NULL"
    )


def downgrade():
    op.drop_constraint("once_run_at", "cron_jobs", type_="check")
    op.drop_constraint("schedule_kind", "cron_jobs", type_="check")
    op.drop_column("cron_jobs", "consumed_at")
    op.drop_column("cron_jobs", "run_at")
    op.drop_column("cron_jobs", "schedule_kind")
