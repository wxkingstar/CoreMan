"""feishu_deliveries.bot_id: lets assistant card layouts be sealed like other content rows."""

import sqlalchemy as sa
from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 改的是在用的表，要拿排他锁：排在长事务后面会让之后对这张表的读写全部跟着排队。
    # 5 秒拿不到锁就报错，整次迁移回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "feishu_deliveries",
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_feishu_deliveries_bot_id_bots"),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE feishu_deliveries d SET bot_id = s.bot_id "
        "FROM task_streams s WHERE s.task_id = d.task_id"
    )


def downgrade() -> None:
    # 改的是在用的表，要拿排他锁：排在长事务后面会让之后对这张表的读写全部跟着排队。
    # 5 秒拿不到锁就报错，整次迁移回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("feishu_deliveries", "bot_id")
