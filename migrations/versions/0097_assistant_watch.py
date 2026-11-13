"""Personal assistant watch: polling state, graded inbox, per-app Feishu rate usage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _now(name: str) -> sa.Column:
    return _ts(name, server_default=sa.text("now()"), nullable=False)


def _jsonb(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), **kw)


def upgrade() -> None:
    op.create_table(
        "assistant_watch",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
        ),
        _jsonb("cursor", server_default=sa.text("'{}'::jsonb"), nullable=False),
        _jsonb("head_enc", nullable=True),
        sa.Column("interval", sa.Integer(), server_default=sa.text("60"), nullable=False),
        _now("next_poll_at"),
        sa.Column("state", sa.Text(), server_default=sa.text("'idle'"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ts("last_ok_at", nullable=True),
        sa.Column("failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("keepalive_at", nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(
            "state IN ('idle', 'polling', 'backoff', 'paused', 'stopped')", name="state"
        ),
        sa.CheckConstraint('"interval" BETWEEN 60 AND 3600', name="interval"),
    )
    op.create_index("assistant_watch_due_idx", "assistant_watch", ["next_poll_at"])
    op.create_table(
        "assistant_inbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.Text(), server_default=sa.text("'message'"), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("thread_id", sa.Text(), nullable=True),
        _ts("message_at", nullable=True),
        _jsonb("summary_enc", nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("void_code", sa.Text(), nullable=True),
        _ts("hold_until", nullable=True),
        sa.Column("delivered_task_id", sa.BigInteger(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint("kind IN ('message', 'approval_todo', 'reminder')", name="kind"),
        sa.CheckConstraint("level IN ('immediate', 'batch', 'hold')", name="level"),
        sa.CheckConstraint("status IN ('pending', 'delivered', 'void')", name="status"),
    )
    op.create_index(
        "assistant_inbox_message_uq",
        "assistant_inbox",
        ["bot_id", "kind", "message_id"],
        unique=True,
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )
    op.create_index(
        "assistant_inbox_due_idx", "assistant_inbox", ["bot_id", "status", "hold_until"]
    )
    op.create_table(
        "feishu_rate_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("app_id", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("units", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _now("at"),
        sa.CheckConstraint("category IN ('chats', 'history')", name="category"),
        sa.CheckConstraint("units > 0", name="units"),
    )
    op.create_index(
        "feishu_rate_usage_window_idx", "feishu_rate_usage", ["app_id", "category", "at"]
    )


def downgrade() -> None:
    op.drop_table("feishu_rate_usage")
    op.drop_table("assistant_inbox")
    op.drop_table("assistant_watch")
