"""按系统设置平台代理的批量上限、单轮调用上限与批量限速；待执行批量项的部分索引。

四列都有服务端默认值，旧进程不读也不写，滚动升级期间无影响。索引在线建（CONCURRENTLY），
不能跑在事务里，放进 autocommit_block。

先建索引再加列：索引带 if_not_exists，加列在事务里；任何一步失败（例如 5 秒拿不到锁）都
可以直接重跑，不会留下「列已加上、版本号没前进」的半截状态。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None

# 列名 → (默认, 最小, 最大)，与 coreman.core.db.models.infrastructure.PROXY_LIMITS 一致。
_LIMITS = {
    "batch_write_max": (1000, 1, 50000),
    "batch_destructive_max": (50, 1, 500),
    "proxy_calls_per_turn": (3000, 1, 20000),
    "batch_interval_ms": (0, 0, 60000),
}
_INDEX = "system_batch_items_pending_idx"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _INDEX,
            "system_batch_items",
            ["batch_id", "seq"],
            postgresql_where=sa.text("status = 'pending'"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name, (default, low, high) in _LIMITS.items():
        op.add_column(
            "systems",
            sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text(str(default))),
        )
        op.create_check_constraint(name, "systems", f"{name} BETWEEN {low} AND {high}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in reversed(_LIMITS):
        op.drop_column("systems", name)
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX, table_name="system_batch_items", postgresql_concurrently=True, if_exists=True
        )
