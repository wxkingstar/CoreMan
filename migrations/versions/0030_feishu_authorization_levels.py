"""Explicit per-bot personal authorization selection, independent of OAuth consent history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "feishu_personal_grants",
        sa.Column("remote_revoked", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "feishu_personal_grants",
        sa.Column(
            "authorization_level", sa.Text(), nullable=False, server_default="legacy_readonly"
        ),
    )
    op.add_column(
        "feishu_personal_grants",
        sa.Column(
            "requested_scopes", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"
        ),
    )
    op.add_column("feishu_personal_grants", sa.Column("selection_chat_id", sa.Text()))


def downgrade():
    for column in (
        "remote_revoked",
        "selection_chat_id",
        "requested_scopes",
        "authorization_level",
    ):
        op.drop_column("feishu_personal_grants", column)
