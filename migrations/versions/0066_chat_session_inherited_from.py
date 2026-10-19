"""Runtime switch keeps the session key: a session that cannot move to the new runtime starts
fresh and remembers the previous relay session, whose recent turns go into the first turn."""

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("inherited_from", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "inherited_from")
