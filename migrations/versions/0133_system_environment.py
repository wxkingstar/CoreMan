"""业务系统的环境属性：production（真实环境）或 test（测试环境）。

已有系统按服务端默认值全部记为 production；旧进程不读也不写这一列，滚动升级期间无影响。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "systems",
        sa.Column("environment", sa.Text(), nullable=False, server_default=sa.text("'production'")),
    )
    op.create_check_constraint("environment", "systems", "environment IN ('production', 'test')")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("systems", "environment")
