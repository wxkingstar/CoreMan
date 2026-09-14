"""Encrypted project access tokens for skill source repositories."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skill_sources", sa.Column("access_token_enc", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("skill_sources", "access_token_enc")
