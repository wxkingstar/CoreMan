"""Work tasks, phase 2: delivery report and artifacts, a waiting run outcome, and the old
statuses dropped (the previous release writes only the new ones)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None

STATUSES = "'shadow','todo','in_progress','waiting','review','done','cancelled'"
LEGACY = "'pending','queued','running','awaiting_confirmation','unfinished','needs_human'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("work_tasks", sa.Column("report", sa.Text(), server_default="", nullable=False))
    op.add_column(
        "work_tasks",
        sa.Column(
            "artifacts",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    # 上一次发布期间还在跑的旧版本可能写过旧状态：再映射一遍，再收紧约束。
    op.execute("UPDATE work_tasks SET status = 'todo' WHERE status = 'pending'")
    op.execute(
        "UPDATE work_tasks SET status = 'waiting', "
        """waiting_on = '{"kind": "queue", "human_text": "排队中"}'::jsonb """
        "WHERE status = 'queued'"
    )
    op.execute("UPDATE work_tasks SET status = 'in_progress' WHERE status = 'running'")
    op.execute(
        "UPDATE work_tasks SET status = 'waiting', closed_at = NULL, "
        "waiting_on = jsonb_build_object('kind', 'human', 'user_id', "
        "COALESCE(principal_user_id, requester_user_id)::text, 'human_text', '等发起人决定') "
        "WHERE status IN ('awaiting_confirmation', 'unfinished', 'needs_human')"
    )
    op.drop_constraint("status", "work_tasks", type_="check")
    op.create_check_constraint("status", "work_tasks", f"status IN ({STATUSES})")
    op.drop_constraint("status", "work_task_runs", type_="check")
    op.create_check_constraint(
        "status",
        "work_task_runs",
        "status IN ('running','done','confirm','failed','cancelled','waiting')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("UPDATE work_task_runs SET status = 'done' WHERE status = 'waiting'")
    op.drop_constraint("status", "work_task_runs", type_="check")
    op.create_check_constraint(
        "status", "work_task_runs", "status IN ('running','done','confirm','failed','cancelled')"
    )
    op.drop_constraint("status", "work_tasks", type_="check")
    op.create_check_constraint("status", "work_tasks", f"status IN ({STATUSES},{LEGACY})")
    op.drop_column("work_tasks", "artifacts")
    op.drop_column("work_tasks", "report")
