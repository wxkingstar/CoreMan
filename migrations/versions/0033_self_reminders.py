"""Server-owned fixed self reminders and scoped confirmation state."""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "cron_jobs", sa.Column("execution_mode", sa.Text(), nullable=False, server_default="ai")
    )
    op.add_column("cron_jobs", sa.Column("reminder_chat_id", sa.Text()))
    op.create_check_constraint(
        "execution_mode", "cron_jobs", "execution_mode IN ('ai', 'self_reminder')"
    )
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint(
        "kind",
        "interaction_states",
        "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder')",
    )


def downgrade():
    op.execute("DELETE FROM interaction_states WHERE kind = 'self_reminder'")
    op.execute("DELETE FROM cron_jobs WHERE execution_mode = 'self_reminder'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint(
        "kind", "interaction_states", "kind IN ('choice', 'relay_switch', 'session_switch')"
    )
    op.drop_constraint("execution_mode", "cron_jobs", type_="check")
    op.drop_column("cron_jobs", "reminder_chat_id")
    op.drop_column("cron_jobs", "execution_mode")
