"""个人助理的托管范围：只托管名单里的会话（`chat_scope`、`managed_chats_enc`）。

两列都有服务端默认值或可空，旧进程不读也不写，滚动升级期间无影响。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "assistant_settings",
        sa.Column("managed_chats_enc", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "assistant_settings",
        sa.Column("chat_scope", sa.Text(), nullable=False, server_default=sa.text("'all'")),
    )
    op.create_check_constraint(
        "chat_scope", "assistant_settings", "chat_scope IN ('all', 'listed')"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("assistant_settings", "chat_scope")
    op.drop_column("assistant_settings", "managed_chats_enc")
