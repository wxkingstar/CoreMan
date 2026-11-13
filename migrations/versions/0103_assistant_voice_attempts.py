"""Personal assistant voice page: rate-limit regeneration attempts, mark one in progress."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_settings",
        sa.Column(
            "voice_attempts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "assistant_settings",
        sa.Column("voice_regenerating_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_settings", "voice_regenerating_at")
    op.drop_column("assistant_settings", "voice_attempts")
