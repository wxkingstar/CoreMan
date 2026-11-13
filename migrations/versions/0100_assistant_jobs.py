"""Personal assistant jobs: sub-tasks in their own session whose result returns to the main one."""

import sqlalchemy as sa
from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

KINDS = "kind IN ('subtask', 'bot_help')"
ORIGINS = "origin IN ('assistant', 'system')"
STATUSES = "status IN ('queued', 'running', 'done', 'failed', 'cancelled', 'blocked')"
LIVE = "status IN ('queued', 'running')"
UNREPORTED = (
    "report AND reported_at IS NULL AND status IN ('done', 'failed', 'cancelled', 'blocked')"
)
FOLLOWUP_FK = "fk_assistant_followups_job_id_assistant_jobs"


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "assistant_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.Text(), server_default=sa.text("'subtask'"), nullable=False),
        sa.Column("origin", sa.Text(), server_default=sa.text("'assistant'"), nullable=False),
        sa.Column(
            "target_bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("title_enc", sa.Text(), nullable=False),
        sa.Column("brief_enc", sa.Text(), nullable=False),
        sa.Column("message_enc", sa.Text(), nullable=True),
        sa.Column("result_enc", sa.Text(), nullable=True),
        sa.Column("blocked_enc", sa.Text(), nullable=True),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("origin_session_key", sa.Text(), nullable=False),
        sa.Column("rounds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_task_id", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("report", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("result_task_id", sa.BigInteger(), nullable=True),
        sa.Column("report_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("report_after", nullable=True),
        sa.Column("report_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("reported_at", nullable=True),
        sa.Column("report_code", sa.Text(), nullable=True),
        _ts("deadline_at", nullable=False),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        _ts("updated_at", server_default=sa.text("now()"), nullable=False),
        _ts("started_at", nullable=True),
        _ts("finished_at", nullable=True),
        sa.CheckConstraint(KINDS, name="kind"),
        sa.CheckConstraint(ORIGINS, name="origin"),
        sa.CheckConstraint(STATUSES, name="status"),
    )
    op.create_index("assistant_jobs_bot_status_idx", "assistant_jobs", ["bot_id", "status"])
    op.create_index(
        "assistant_jobs_live_idx",
        "assistant_jobs",
        ["deadline_at"],
        postgresql_where=sa.text(LIVE),
    )
    op.create_index(
        "assistant_jobs_unreported_idx",
        "assistant_jobs",
        ["bot_id"],
        postgresql_where=sa.text(UNREPORTED),
    )
    # 2C 留下的普通列现在有了去处；此前没有任何代码写过它，为防万一先清掉指不到的值。
    op.execute("UPDATE assistant_followups SET job_id = NULL WHERE job_id IS NOT NULL")
    op.create_foreign_key(
        FOLLOWUP_FK,
        "assistant_followups",
        "assistant_jobs",
        ["job_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(FOLLOWUP_FK, "assistant_followups", type_="foreignkey")
    op.drop_index("assistant_jobs_unreported_idx", table_name="assistant_jobs")
    op.drop_index("assistant_jobs_live_idx", table_name="assistant_jobs")
    op.drop_index("assistant_jobs_bot_status_idx", table_name="assistant_jobs")
    op.drop_table("assistant_jobs")
