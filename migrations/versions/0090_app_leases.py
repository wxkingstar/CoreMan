"""Connection leases for personal assistant apps: one long connection per app."""

import sqlalchemy as sa
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

STATES = "connection_state IN ('disconnected', 'connecting', 'subscribed', 'kicked', 'auth_failed')"


def upgrade() -> None:
    op.create_table(
        "app_leases",
        sa.Column(
            "app_id",
            sa.Uuid(),
            sa.ForeignKey("platform_apps.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "holder_instance",
            sa.Text(),
            sa.ForeignKey("process_instances.id", ondelete="SET NULL"),
        ),
        sa.Column("generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("drain_requested_by", sa.Text()),
        sa.Column("drain_requested_at", sa.DateTime(timezone=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column(
            "connection_state",
            sa.Text(),
            server_default=sa.text("'disconnected'"),
            nullable=False,
        ),
        sa.CheckConstraint(STATES, name="connection_state"),
    )


def downgrade() -> None:
    op.drop_table("app_leases")
