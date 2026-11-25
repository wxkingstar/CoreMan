"""Work tasks become a company task platform: people can be assignees, tasks track a principal,
review, a pending choice, start mode and agent runs; statuses are reduced to what people act on."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None

NEW_STATUSES = "'shadow','todo','in_progress','waiting','review','done','cancelled'"
# 发布期间上一版本的 worker 还会写旧状态：这一版两套都允许，旧的在下一版迁移里去掉。
LEGACY_STATUSES = "'pending','queued','running','awaiting_confirmation','unfinished','needs_human'"
OLD_STATUSES = (
    "'shadow','pending','queued','running','awaiting_confirmation',"
    "'done','unfinished','cancelled','needs_human'"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    add = op.add_column
    add("work_tasks", sa.Column("source", sa.Text(), server_default="dispatch", nullable=False))
    add("work_tasks", sa.Column("requester_bot_id", sa.Uuid(), nullable=True))
    add("work_tasks", sa.Column("principal_user_id", sa.Uuid(), nullable=True))
    add("work_tasks", sa.Column("assignee_user_id", sa.Uuid(), nullable=True))
    add("work_tasks", sa.Column("reviewer_user_id", sa.Uuid(), nullable=True))
    add(
        "work_tasks",
        sa.Column("review_required", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    add("work_tasks", sa.Column("root_id", sa.BigInteger(), nullable=True))
    add("work_tasks", sa.Column("depth", sa.Integer(), server_default="0", nullable=False))
    add("work_tasks", sa.Column("waiting_on", postgresql.JSONB(), nullable=True))
    add("work_tasks", sa.Column("ask", postgresql.JSONB(), nullable=True))
    add("work_tasks", sa.Column("priority", sa.Text(), server_default="normal", nullable=False))
    add("work_tasks", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    add("work_tasks", sa.Column("start_mode", sa.Text(), server_default="auto", nullable=False))
    add("work_tasks", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("work_tasks", "platform", nullable=True)
    op.alter_column("work_tasks", "chat_id", nullable=True)
    for column, table in (
        ("requester_bot_id", "bots"),
        ("principal_user_id", "users"),
        ("assignee_user_id", "users"),
        ("reviewer_user_id", "users"),
    ):
        op.create_foreign_key(
            f"fk_work_tasks_{column}_{table}",
            "work_tasks",
            table,
            [column],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_foreign_key(
        "fk_work_tasks_root_id_work_tasks",
        "work_tasks",
        "work_tasks",
        ["root_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "source", "work_tasks", "source IN ('dispatch','web','agent','api','collaboration')"
    )
    op.create_check_constraint("priority", "work_tasks", "priority IN ('normal','high','urgent')")
    op.create_check_constraint("start_mode", "work_tasks", "start_mode IN ('auto','manual')")
    op.create_check_constraint(
        "one_assignee",
        "work_tasks",
        "assignee_bot_id IS NULL OR assignee_user_id IS NULL",
    )

    # 旧状态迁到新状态：卡住、没做完、等确认的都落在「等发起人」，以前的发起人就是代表人。
    op.drop_constraint("status", "work_tasks", type_="check")
    op.execute("UPDATE work_tasks SET principal_user_id = requester_user_id")
    op.execute("UPDATE work_tasks SET status = 'todo' WHERE status = 'pending'")
    op.execute(
        "UPDATE work_tasks SET status = 'waiting', "
        """waiting_on = '{"kind": "queue", "human_text": "排队中"}'::jsonb """
        "WHERE status = 'queued'"
    )
    op.execute("UPDATE work_tasks SET status = 'in_progress' WHERE status = 'running'")
    op.execute(
        "UPDATE work_tasks SET status = 'waiting', closed_at = NULL, "
        "waiting_on = jsonb_build_object('kind', 'human', 'user_id', requester_user_id::text, "
        "'human_text', '等发起人确认') WHERE status = 'awaiting_confirmation'"
    )
    op.execute(
        "UPDATE work_tasks SET status = 'waiting', closed_at = NULL, "
        "waiting_on = jsonb_build_object('kind', 'human', 'user_id', requester_user_id::text, "
        "'human_text', '没做完，等发起人决定') WHERE status IN ('unfinished', 'needs_human')"
    )
    op.create_check_constraint(
        "status", "work_tasks", f"status IN ({NEW_STATUSES},{LEGACY_STATUSES})"
    )

    op.create_index("work_tasks_assignee_user_idx", "work_tasks", ["assignee_user_id", "status"])
    op.create_index("work_tasks_assignee_bot_idx", "work_tasks", ["assignee_bot_id", "status"])
    op.create_index("work_tasks_reviewer_idx", "work_tasks", ["reviewer_user_id"])
    op.create_index("work_tasks_principal_idx", "work_tasks", ["principal_user_id"])
    op.create_index("work_tasks_requester_idx", "work_tasks", ["requester_user_id"])
    op.create_index("work_tasks_parent_idx", "work_tasks", ["parent_id"])

    op.create_table(
        "work_task_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("work_task_id", sa.BigInteger(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=True),
        sa.Column("principal_user_id", sa.Uuid(), nullable=True),
        sa.Column("chat_task_id", sa.BigInteger(), nullable=False),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column(
            "triggered_by",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("outcome", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','done','confirm','failed','cancelled')", name="status"
        ),
        sa.ForeignKeyConstraint(["work_task_id"], ["work_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["principal_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_task_id", name="uq_work_task_runs_chat_task_id"),
    )
    op.create_index("work_task_runs_task_idx", "work_task_runs", ["work_task_id", "started_at"])
    op.create_table(
        "work_task_watchers",
        sa.Column("work_task_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["work_task_id"], ["work_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("work_task_id", "user_id"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("work_task_watchers")
    op.drop_index("work_task_runs_task_idx", table_name="work_task_runs")
    op.drop_table("work_task_runs")
    for name in (
        "work_tasks_parent_idx",
        "work_tasks_requester_idx",
        "work_tasks_principal_idx",
        "work_tasks_reviewer_idx",
        "work_tasks_assignee_bot_idx",
        "work_tasks_assignee_user_idx",
    ):
        op.drop_index(name, table_name="work_tasks")
    op.drop_constraint("status", "work_tasks", type_="check")
    op.execute("UPDATE work_tasks SET status = 'pending' WHERE status = 'todo'")
    op.execute("UPDATE work_tasks SET status = 'running' WHERE status = 'in_progress'")
    op.execute(
        "UPDATE work_tasks SET status = 'queued' "
        "WHERE status = 'waiting' AND waiting_on->>'kind' = 'queue'"
    )
    op.execute("UPDATE work_tasks SET status = 'awaiting_confirmation' WHERE status = 'waiting'")
    op.execute("UPDATE work_tasks SET status = 'awaiting_confirmation' WHERE status = 'review'")
    op.create_check_constraint("status", "work_tasks", f"status IN ({OLD_STATUSES})")
    # 网页上建的任务没有群：旧结构里这两列必填。
    op.execute("UPDATE work_tasks SET platform = 'feishu' WHERE platform IS NULL")
    op.execute("UPDATE work_tasks SET chat_id = '' WHERE chat_id IS NULL")
    for name in ("one_assignee", "start_mode", "priority", "source"):
        op.drop_constraint(name, "work_tasks", type_="check")
    op.drop_constraint("fk_work_tasks_root_id_work_tasks", "work_tasks", type_="foreignkey")
    for column, table in (
        ("requester_bot_id", "bots"),
        ("principal_user_id", "users"),
        ("assignee_user_id", "users"),
        ("reviewer_user_id", "users"),
    ):
        op.drop_constraint(f"fk_work_tasks_{column}_{table}", "work_tasks", type_="foreignkey")
    op.alter_column("work_tasks", "chat_id", nullable=False)
    op.alter_column("work_tasks", "platform", nullable=False)
    for column in (
        "claimed_at",
        "start_mode",
        "due_at",
        "priority",
        "ask",
        "waiting_on",
        "depth",
        "root_id",
        "review_required",
        "reviewer_user_id",
        "assignee_user_id",
        "principal_user_id",
        "requester_bot_id",
        "source",
    ):
        op.drop_column("work_tasks", column)
