"""Personal assistant hardening: the watch cursor becomes a sealed column."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The plaintext cursor cannot be encrypted here (the data keys are wrapped by the master
    # key, which migrations do not have): drop it and let each assistant record a new baseline.
    op.add_column(
        "assistant_watch",
        sa.Column(
            "cursor_enc",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.drop_column("assistant_watch", "cursor")


def downgrade() -> None:
    op.add_column(
        "assistant_watch",
        sa.Column(
            "cursor",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.drop_column("assistant_watch", "cursor_enc")
