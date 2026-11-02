"""AI employee help without partner setup: peers are found in the current group, and each
employee has one switch for whether others may ask it. Partner tables stay as history."""

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bots",
        sa.Column("accept_bot_help", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("bots", "accept_bot_help")
