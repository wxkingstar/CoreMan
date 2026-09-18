"""Server-side QR sessions that create a WeCom intelligent bot for a new employee."""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

STATUSES = ("pending", "succeeded", "consumed", "expired", "failed", "cancelled")


def _check(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade():
    op.create_table(
        "wecom_bot_provisions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE")),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("pending_enc", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("poll_interval", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_poll_at", sa.DateTime(timezone=True)),
        sa.Column("upstream_status", sa.Text()),
        sa.Column("wecom_bot_id", sa.Text()),
        sa.Column("secret_enc", sa.Text()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_check("status", STATUSES), name="status"),
    )
    op.create_index(
        "ix_wecom_bot_provisions_user_id", "wecom_bot_provisions", ["user_id", "created_at"]
    )
    op.create_index("ix_wecom_bot_provisions_bot_id", "wecom_bot_provisions", ["bot_id"])
    op.create_index(
        "ix_wecom_bot_provisions_due",
        "wecom_bot_provisions",
        ["next_poll_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # The alert rule looks at recently touched sessions, so keep updated_at honest.
    op.execute(
        "CREATE TRIGGER trg_wecom_bot_provisions_updated_at BEFORE UPDATE ON wecom_bot_provisions "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade():
    op.drop_table("wecom_bot_provisions")
