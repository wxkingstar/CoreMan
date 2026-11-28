"""`chat_sessions.lineage`: the relay sessions a session key went through since it last started
fresh, oldest first, as `{"id", "backend"}`.

After a runtime switch that could not carry the native session, the first turn gets the recent
conversation as text. It used to read only the previous relay session, so switching away and
back lost everything said before the first switch. Additive only: the previous version never
reads the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "chat_sessions",
        sa.Column(
            "lineage",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "lineage")
