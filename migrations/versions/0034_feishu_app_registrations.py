"""Server-side QR registrations that create or update a bot's Feishu application."""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

PURPOSES = ("create", "update")
STATUSES = ("pending", "succeeded", "consumed", "expired", "denied", "failed", "cancelled")


def _check(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade():
    op.create_table(
        "feishu_app_registrations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE")),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("pending_enc", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("poll_interval", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_poll_at", sa.DateTime(timezone=True)),
        sa.Column("app_id", sa.Text()),
        sa.Column("secret_enc", sa.Text()),
        sa.Column("owner_open_id", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_check("purpose", PURPOSES), name="purpose"),
        sa.CheckConstraint(_check("status", STATUSES), name="status"),
    )
    op.create_index(
        "ix_feishu_app_registrations_user_id", "feishu_app_registrations", ["user_id", "created_at"]
    )
    op.create_index("ix_feishu_app_registrations_bot_id", "feishu_app_registrations", ["bot_id"])


def downgrade():
    op.drop_table("feishu_app_registrations")
