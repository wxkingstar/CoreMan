"""Proxied business-system writes wait for a confirmation card (interaction kind systems_write);
large or file responses are kept in short-term object storage (system_calls.object_id)."""

import sqlalchemy as sa
from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None

BEFORE = (
    "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
    " 'personal_schedule', 'feishu_group_write')"
)
AFTER = (
    "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
    " 'personal_schedule', 'feishu_group_write', 'systems_write')"
)


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint("kind", "interaction_states", AFTER)
    op.add_column("system_calls", sa.Column("object_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("system_calls", "object_id")
    op.execute("DELETE FROM interaction_states WHERE kind = 'systems_write'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint("kind", "interaction_states", BEFORE)
