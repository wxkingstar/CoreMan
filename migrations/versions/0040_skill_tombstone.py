"""Deleted catalog skills keep a tombstone so a later source sync does not import them again."""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("skills", sa.Column("deleted_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("skills", "deleted_at")
