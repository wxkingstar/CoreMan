"""chat_sessions.forked_from: a Feishu topic session forked from the group's main session."""

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("forked_from", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "forked_from")
