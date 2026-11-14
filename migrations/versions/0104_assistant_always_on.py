"""Personal assistant settings: work around the clock by default (always_on), and allow a
personal daily turn budget up to 20000."""

import sqlalchemy as sa
from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None

TABLE = "assistant_settings"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("always_on", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.drop_constraint("daily_turn_budget", TABLE, type_="check")
    op.create_check_constraint(
        "daily_turn_budget",
        TABLE,
        "daily_turn_budget IS NULL OR daily_turn_budget BETWEEN 10 AND 20000",
    )


def downgrade() -> None:
    op.drop_constraint("daily_turn_budget", TABLE, type_="check")
    op.execute(f"UPDATE {TABLE} SET daily_turn_budget = 2000 WHERE daily_turn_budget > 2000")
    op.create_check_constraint(
        "daily_turn_budget",
        TABLE,
        "daily_turn_budget IS NULL OR daily_turn_budget BETWEEN 10 AND 2000",
    )
    op.drop_column(TABLE, "always_on")
