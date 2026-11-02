"""Group dispatcher and work tasks: a per-bot group role (member / dispatcher), the dispatch
result on observe decisions, and the work task ledger with its two-layer event timeline."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bots",
        sa.Column("group_role", sa.Text(), nullable=False, server_default=sa.text("'member'")),
    )
    op.create_check_constraint("group_role", "bots", "group_role IN ('member','dispatcher')")
    op.add_column(
        "group_observe_decisions",
        sa.Column("dispatch", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute("CREATE SEQUENCE work_task_code_seq")
    op.create_table(
        "work_tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "code",
            sa.Text(),
            nullable=False,
            server_default=sa.text("('T-'::text || nextval('work_task_code_seq'::regclass))"),
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("origin_event_id", sa.BigInteger(), nullable=True),
        sa.Column("requester_user_id", sa.Uuid(), nullable=True),
        sa.Column("requester_name", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("assignee_bot_id", sa.Uuid(), nullable=True),
        sa.Column("dispatcher_bot_id", sa.Uuid(), nullable=True),
        sa.Column("decision_id", sa.BigInteger(), nullable=True),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "related_codes",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("brief", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("digest", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('shadow','pending','queued','running','awaiting_confirmation',"
            "'done','unfinished','cancelled','needs_human')",
            name="status",
        ),
        sa.ForeignKeyConstraint(["requester_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assignee_bot_id"], ["bots.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dispatcher_bot_id"], ["bots.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_id"], ["work_tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_work_tasks_code"),
    )
    op.create_index("work_tasks_chat_idx", "work_tasks", ["chat_id", "created_at"])
    op.create_index("work_tasks_created_idx", "work_tasks", ["created_at"])
    op.create_table(
        "work_task_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("work_task_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("human_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "agent_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("chat_task_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("actor_type IN ('human','bot','system')", name="actor_type"),
        sa.ForeignKeyConstraint(["work_task_id"], ["work_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("work_task_events_task_idx", "work_task_events", ["work_task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("work_task_events_task_idx", table_name="work_task_events")
    op.drop_table("work_task_events")
    op.drop_index("work_tasks_created_idx", table_name="work_tasks")
    op.drop_index("work_tasks_chat_idx", table_name="work_tasks")
    op.drop_table("work_tasks")
    op.execute("DROP SEQUENCE work_task_code_seq")
    op.drop_column("group_observe_decisions", "dispatch")
    op.drop_constraint("group_role", "bots", type_="check")
    op.drop_column("bots", "group_role")
