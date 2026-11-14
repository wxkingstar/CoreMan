"""Personal assistant settings: work around the clock by default (always_on)."""

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


def downgrade() -> None:
    op.drop_column(TABLE, "always_on")
