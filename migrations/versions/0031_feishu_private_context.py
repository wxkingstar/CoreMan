"""Separate private conversation generations and explicit assistant mode."""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "feishu_personal_grants",
        sa.Column(
            "context_epoch", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
    )
    op.add_column(
        "feishu_personal_grants",
        sa.Column("assistant_mode", sa.Text(), nullable=False, server_default="personal"),
    )


def downgrade():
    op.drop_column("feishu_personal_grants", "assistant_mode")
    op.drop_column("feishu_personal_grants", "context_epoch")
