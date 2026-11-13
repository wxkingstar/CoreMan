"""Personal assistant provisioning and deprovisioning: record state, lifecycles, retired dirs."""

import sqlalchemy as sa
from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None

STATE_CHECK = (
    "assistant_state IS NULL OR (kind = 'assistant' AND assistant_state IN "
    "('provisioning', 'active', 'deprovisioning', 'deprovisioned'))"
)
STEPS = (
    "step IN ('offered', 'create', 'init_workspace', 'start_grant', 'await_grant', 'grace',"
    " 'drain', 'withdraw', 'revoke', 'destroy')"
)


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    # 改的是在用的表，要拿排他锁：排在长事务后面会让之后对这张表的读写全部跟着排队。
    # 5 秒拿不到锁就报错，整次迁移回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("bots", sa.Column("assistant_state", sa.Text(), nullable=True))
    # 已有的助理记录（本功能上线前建的）按已开通处理；普通记录保持为空。
    op.execute("UPDATE bots SET assistant_state = 'active' WHERE kind = 'assistant'")
    op.create_check_constraint("assistant_state", "bots", STATE_CHECK)
    op.create_table(
        "assistant_lifecycles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_assistant_lifecycles_owner"),
            nullable=False,
        ),
        sa.Column(
            "platform_app_id",
            sa.Uuid(),
            sa.ForeignKey(
                "platform_apps.id", ondelete="CASCADE", name="fk_assistant_lifecycles_app"
            ),
            nullable=False,
        ),
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="SET NULL", name="fk_assistant_lifecycles_bot"),
            nullable=True,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=True),
        sa.Column(
            "actor_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_assistant_lifecycles_actor"),
            nullable=True,
        ),
        sa.Column("platform_user_id", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("offer_hash", sa.Text(), nullable=True),
        sa.Column("offer_message_id", sa.Text(), nullable=True),
        sa.Column("offer_chat_id", sa.Text(), nullable=True),
        sa.Column("open_id", sa.Text(), nullable=True),
        sa.Column("tenant_key", sa.Text(), nullable=True),
        _ts("deadline", nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("issued", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("next_attempt_at", nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        _ts("updated_at", server_default=sa.text("now()"), nullable=False),
        _ts("finished_at", nullable=True),
        sa.CheckConstraint("action IN ('provision', 'deprovision')", name="action"),
        sa.CheckConstraint(STEPS, name="step"),
        sa.CheckConstraint(
            "trigger IS NULL OR trigger IN ('owner', 'member_left', 'admin', 'abandoned')",
            name="trigger",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('done', 'expired', 'cancelled', 'failed',"
            " 'ineligible')",
            name="outcome",
        ),
    )
    op.create_index(
        "assistant_lifecycles_live_uq",
        "assistant_lifecycles",
        ["owner_id"],
        unique=True,
        postgresql_where=sa.text("finished_at IS NULL"),
    )
    op.create_index(
        "assistant_lifecycles_due_idx",
        "assistant_lifecycles",
        ["next_attempt_at"],
        postgresql_where=sa.text("finished_at IS NULL"),
    )
    op.create_index("assistant_lifecycles_bot_idx", "assistant_lifecycles", ["bot_id"])
    op.create_table(
        "assistant_retired_dirs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column(
            "relay_server_id",
            sa.Uuid(),
            sa.ForeignKey(
                "relay_servers.id", ondelete="CASCADE", name="fk_assistant_retired_dirs_relay"
            ),
            nullable=False,
        ),
        sa.Column("working_dir", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("next_attempt_at", server_default=sa.text("now()"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint(
            "relay_server_id", "working_dir", name="uq_assistant_retired_dirs_path"
        ),
    )
    op.create_index("assistant_retired_dirs_due_idx", "assistant_retired_dirs", ["next_attempt_at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 旧版本没有状态这道闸：开通没走完、正在停用或已停用的助理记录先关掉，免得降级后照常跑轮次。
    op.execute(
        "UPDATE bots SET enabled = false"
        " WHERE kind = 'assistant' AND assistant_state IS DISTINCT FROM 'active'"
    )
    op.drop_index("assistant_retired_dirs_due_idx", table_name="assistant_retired_dirs")
    op.drop_table("assistant_retired_dirs")
    op.drop_index("assistant_lifecycles_bot_idx", table_name="assistant_lifecycles")
    op.drop_index("assistant_lifecycles_due_idx", table_name="assistant_lifecycles")
    op.drop_index("assistant_lifecycles_live_uq", table_name="assistant_lifecycles")
    op.drop_table("assistant_lifecycles")
    op.drop_constraint("assistant_state", "bots", type_="check")
    op.drop_column("bots", "assistant_state")
