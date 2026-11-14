"""Personal assistants interrupt less: what unattended turns want the owner to know is kept in
assistant_reports and sent as a digest at the owner's digest times (default 09:00, 13:30, 18:00)
outside quiet hours (default 22:00–08:00, on for existing assistants too); follow-ups that need
the owner present are flagged (needs_owner); marks remember the turn (task_id) and the subtask or
dispatch that continues it (job_id) so the platform can lift them when that work ends."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None

HHMM = "'^([01][0-9]|2[0-3]):[0-5][0-9]$'"


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "assistant_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("text_enc", sa.Text(), nullable=False),
        sa.Column("urgent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "assistant_reports_unsent_idx",
        "assistant_reports",
        ["bot_id"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    table = "assistant_settings"
    op.add_column(
        table,
        sa.Column(
            "digest_times",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{09:00,13:30,18:00}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column(
        table,
        sa.Column("quiet_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        table,
        sa.Column("quiet_start", sa.Text(), server_default=sa.text("'22:00'"), nullable=False),
    )
    op.add_column(
        table, sa.Column("quiet_end", sa.Text(), server_default=sa.text("'08:00'"), nullable=False)
    )
    op.add_column(table, sa.Column("digest_next_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(table, sa.Column("digest_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("digest_times", table, "cardinality(digest_times) BETWEEN 1 AND 6")
    op.create_check_constraint(
        "quiet_hours",
        table,
        f"quiet_start ~ {HHMM} AND quiet_end ~ {HHMM} AND quiet_start <> quiet_end",
    )
    op.add_column(
        "assistant_followups",
        sa.Column("needs_owner", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("assistant_marks", sa.Column("task_id", sa.BigInteger(), nullable=True))
    op.add_column("assistant_marks", sa.Column("job_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_assistant_marks_job_id_assistant_jobs",
        "assistant_marks",
        "assistant_jobs",
        ["job_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(
        "fk_assistant_marks_job_id_assistant_jobs", "assistant_marks", type_="foreignkey"
    )
    op.drop_column("assistant_marks", "job_id")
    op.drop_column("assistant_marks", "task_id")
    op.drop_column("assistant_followups", "needs_owner")
    table = "assistant_settings"
    op.drop_constraint("quiet_hours", table, type_="check")
    op.drop_constraint("digest_times", table, type_="check")
    for column in (
        "digest_sent_at",
        "digest_next_at",
        "quiet_end",
        "quiet_start",
        "quiet_enabled",
        "digest_times",
    ):
        op.drop_column(table, column)
    op.drop_index("assistant_reports_unsent_idx", table_name="assistant_reports")
    op.drop_table("assistant_reports")
