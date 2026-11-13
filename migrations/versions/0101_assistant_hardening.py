"""Personal assistant hardening: sealed watch cursor, approval revision time, handover pacing."""

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
    op.add_column(
        "assistant_approvals", sa.Column("revised_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column(
        "assistant_inbox",
        sa.Column("handovers", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "assistant_delivery",
        sa.Column("handover_misses", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "assistant_delivery",
        sa.Column("handover_retry_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_delivery", "handover_retry_at")
    op.drop_column("assistant_delivery", "handover_misses")
    op.drop_column("assistant_inbox", "handovers")
    op.drop_column("assistant_approvals", "revised_at")
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
