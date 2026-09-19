"""Per-member WeCom authorization: each member scans once to create a personal bot that only
fetches their data, and every WeCom assistant uses it in that member's private chat.

wecom_personal_grants is left in place for the previous version during a rolling upgrade; the
new code no longer reads it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

STATUSES = ("unbound", "bound")
LEVELS = ("readonly", "all_except_send", "all")
SCAN_STATUSES = ("pending", "succeeded", "expired", "failed", "cancelled")


def _check(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade():
    op.create_table(
        "wecom_personal_bindings",
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="unbound"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("authorization_level", sa.Text(), nullable=False, server_default="readonly"),
        sa.Column(
            "context_epoch", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("wecom_bot_id", sa.Text()),
        sa.Column("credentials_enc", sa.Text()),
        sa.Column("bot_fingerprint", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_enc", sa.Text()),
        sa.Column("authorizer_id", sa.Text()),
        sa.Column("authorizer_name", sa.Text()),
        sa.Column("bot_name", sa.Text()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("bound_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column("capabilities", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("reminded_at", sa.DateTime(timezone=True)),
        sa.Column("scan_status", sa.Text()),
        sa.Column("scan_enc", sa.Text()),
        sa.Column("scan_expires_at", sa.DateTime(timezone=True)),
        sa.Column("scan_next_poll_at", sa.DateTime(timezone=True)),
        sa.Column("scan_poll_interval", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("scan_upstream_status", sa.Text()),
        sa.Column("scan_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_check("status", STATUSES), name="status"),
        sa.CheckConstraint(_check("authorization_level", LEVELS), name="authorization_level"),
        sa.CheckConstraint(
            f"scan_status IS NULL OR {_check('scan_status', SCAN_STATUSES)}", name="scan_status"
        ),
    )
    op.create_index(
        "ix_wecom_personal_bindings_scan_due",
        "wecom_personal_bindings",
        ["scan_next_poll_at"],
        postgresql_where=sa.text("scan_status = 'pending'"),
    )
    op.execute(
        "CREATE TRIGGER trg_wecom_personal_bindings_updated_at BEFORE UPDATE ON "
        "wecom_personal_bindings FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade():
    op.drop_table("wecom_personal_bindings")
