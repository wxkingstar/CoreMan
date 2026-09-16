"""Persist temporary typing reactions independently of card retries."""

import importlib

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    # Both historical branches used 0025. Repair the missing collaboration columns
    # when upgrading a reactions-only database without resetting its version stamp.
    importlib.import_module("migrations.versions.0025_collaboration_setup").upgrade()
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("feishu_deliveries")}
    if "reaction_id" not in existing:
        op.add_column("feishu_deliveries", sa.Column("reaction_id", sa.Text()))
    if "reaction_done" not in existing:
        op.add_column(
            "feishu_deliveries",
            sa.Column(
                "reaction_done", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
        )
    if "reaction_retry_at" not in existing:
        op.add_column(
            "feishu_deliveries", sa.Column("reaction_retry_at", sa.DateTime(timezone=True))
        )
    if "reaction_failures" not in existing:
        op.add_column(
            "feishu_deliveries",
            sa.Column(
                "reaction_failures", sa.BigInteger(), nullable=False, server_default=sa.text("0")
            ),
        )


def downgrade():
    for name in ("reaction_failures", "reaction_retry_at", "reaction_done", "reaction_id"):
        op.drop_column("feishu_deliveries", name)
