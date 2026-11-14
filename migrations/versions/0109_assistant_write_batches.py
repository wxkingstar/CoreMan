"""Personal assistants' Feishu writes are confirmed per turn: with the owner present they wait for
a confirmation card (interaction kind assistant_write); without him a turn's writes form one
approval in the approval system, its items sharing assistant_approvals.batch_id (the id of the
first item, which carries the approval system's external id)."""

import sqlalchemy as sa
from alembic import op

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None

KINDS_BEFORE = (
    "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
    " 'personal_schedule', 'feishu_group_write', 'systems_write', 'cloud_write')"
)
KINDS_AFTER = (
    "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
    " 'personal_schedule', 'feishu_group_write', 'systems_write', 'cloud_write',"
    " 'assistant_write')"
)


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("assistant_approvals", sa.Column("batch_id", sa.Uuid(), nullable=True))
    op.create_index(
        "assistant_approvals_batch_idx",
        "assistant_approvals",
        ["batch_id"],
        postgresql_where=sa.text("batch_id IS NOT NULL"),
    )
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint("kind", "interaction_states", KINDS_AFTER)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 暂存在确认卡里、还没确认的飞书写操作随之作废（降级后的代码不认识它们）。
    op.execute("DELETE FROM interaction_states WHERE kind = 'assistant_write'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint("kind", "interaction_states", KINDS_BEFORE)
    # 降级后的代码不认识合单：除第一项外还在途的项收不到决定，直接作废，不会被执行；第一项的
    # 令牌按合单的请求体签发，旧代码核销对不上，记失败。
    op.execute(
        "UPDATE assistant_approvals SET status = 'withdrawn', error_code = 'downgraded',"
        " decided_at = now() WHERE batch_id IS NOT NULL AND batch_id <> id"
        " AND status IN ('pending', 'approved')"
    )
    op.drop_index("assistant_approvals_batch_idx", table_name="assistant_approvals")
    op.drop_column("assistant_approvals", "batch_id")
