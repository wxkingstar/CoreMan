"""Feishu personal tools become additive; owners may schedule their own AI jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade():
    # feishu_personal_grants.assistant_mode is no longer read, but the previous release still
    # writes it during rollout; it is dropped by a later migration.
    op.drop_constraint("execution_mode", "cron_jobs", type_="check")
    op.create_check_constraint(
        "execution_mode", "cron_jobs", "execution_mode IN ('ai', 'self_reminder', 'personal_ai')"
    )
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint(
        "kind",
        "interaction_states",
        "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
        " 'personal_schedule')",
    )
    # A run that acted as its owner (or belongs to an owner-only job) is visible only to them.
    op.add_column(
        "cron_runs",
        sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("cron_runs", "private")
    op.execute("DELETE FROM interaction_states WHERE kind = 'personal_schedule'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint(
        "kind",
        "interaction_states",
        "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder')",
    )
    op.execute("DELETE FROM cron_jobs WHERE execution_mode = 'personal_ai'")
    op.drop_constraint("execution_mode", "cron_jobs", type_="check")
    op.create_check_constraint(
        "execution_mode", "cron_jobs", "execution_mode IN ('ai', 'self_reminder')"
    )
