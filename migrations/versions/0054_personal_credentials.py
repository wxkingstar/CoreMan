"""Per-member credentials an agent asks for through a secure form, and the requests themselves.

New tables only: the previous version never reads or writes them during a rolling upgrade.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

STATUSES = ("open", "submitted", "expired", "cancelled")
ORIGINS = ("chat", "cron")


def _check(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _timestamps():
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade():
    op.create_table(
        "personal_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("env_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("secret", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("value_enc", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("bot_id", "user_id", "env_key", name="uq_personal_credentials_bot_id"),
    )
    op.create_index("personal_credentials_user_idx", "personal_credentials", ["user_id"])
    op.create_table(
        "credential_requests",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("origin_kind", sa.Text(), nullable=False),
        sa.Column("origin_task_id", sa.BigInteger()),
        sa.Column("origin_event_id", sa.BigInteger()),
        sa.Column("origin_chat_id", sa.Text(), nullable=False),
        sa.Column("origin_chat_type", sa.Text(), nullable=False),
        sa.Column("origin_session_key", sa.Text()),
        sa.Column("cron_job_id", sa.Uuid()),
        sa.Column("delivery_chat_id", sa.Text()),
        sa.Column("fields", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "request_outbox_id",
            sa.BigInteger(),
            sa.ForeignKey("outbox.id", ondelete="SET NULL"),
        ),
        sa.Column("resume_task_id", sa.BigInteger()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(_check("status", STATUSES), name="status"),
        sa.CheckConstraint(_check("origin_kind", ORIGINS), name="origin_kind"),
    )
    op.create_index("credential_requests_owner_idx", "credential_requests", ["bot_id", "user_id"])
    op.create_index(
        "credential_requests_open_idx",
        "credential_requests",
        ["expires_at"],
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade():
    op.drop_table("credential_requests")
    op.drop_table("personal_credentials")
