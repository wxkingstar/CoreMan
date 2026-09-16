"""Administrator collaboration route verification metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    # Older QA deployments can carry newer columns with the historical 0023 stamp.
    # Preserve their route identities and ledger while advancing the revision.
    existing = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("bot_collaboration_routes")
    }
    for column in (
        sa.Column("setup", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    ):
        if column.name not in existing:
            op.add_column("bot_collaboration_routes", column)


def downgrade():
    for name in ("version", "archived", "setup"):
        op.drop_column("bot_collaboration_routes", name)
