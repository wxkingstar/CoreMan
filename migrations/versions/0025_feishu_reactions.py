"""Persist temporary typing reactions independently of card retries."""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("feishu_deliveries", sa.Column("reaction_id", sa.Text()))
    op.add_column(
        "feishu_deliveries",
        sa.Column("reaction_done", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("feishu_deliveries", sa.Column("reaction_retry_at", sa.DateTime(timezone=True)))
    op.add_column(
        "feishu_deliveries",
        sa.Column(
            "reaction_failures", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
    )


def downgrade():
    for name in ("reaction_failures", "reaction_retry_at", "reaction_done", "reaction_id"):
        op.drop_column("feishu_deliveries", name)
