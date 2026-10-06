"""Per-business-system token provider selection."""

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "systems", sa.Column("token_provider", sa.Text(), nullable=False, server_default="builtin")
    )
    op.add_column("systems", sa.Column("token_audience", sa.Text()))
    op.add_column("systems", sa.Column("access_test_url", sa.Text()))


def downgrade():
    op.drop_column("systems", "access_test_url")
    op.drop_column("systems", "token_audience")
    op.drop_column("systems", "token_provider")
