"""飞书个人授权的消息范围（`message_range`）：只群聊、只单聊时少申请另一类会话的补充读权限。

有服务端默认值，旧进程不读也不写，滚动升级期间无影响。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "feishu_personal_grants",
        sa.Column("message_range", sa.Text(), nullable=False, server_default=sa.text("'all'")),
    )
    op.create_check_constraint(
        "message_range", "feishu_personal_grants", "message_range IN ('all', 'group', 'p2p')"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("feishu_personal_grants", "message_range")
