"""出站投递与保留期清理用到的部分索引。

- `outbox_active_idx`：认领语句的外层与同组 NOT EXISTS、scheduler 回收 sending 都只看还没落定
  的行，数量很少，不该随历史条目线性变慢；
- `outbox_failed_idx`：失败排查、告警计数与失败 / 跳过条目的保留期清理；
- `tasks_inbound_event_idx`：删入站事件时外键检查要按 inbound_event_id 反查任务。

在线建索引（CONCURRENTLY）不能跑在事务里，所以放进 autocommit_block，不阻塞线上写入。
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_INDEXES = (
    ("outbox_active_idx", "outbox", ["bot_id", "id"], "status IN ('pending','sending')"),
    ("outbox_failed_idx", "outbox", ["bot_id", "id"], "status IN ('failed','skipped')"),
    ("tasks_inbound_event_idx", "tasks", ["inbound_event_id"], "inbound_event_id IS NOT NULL"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, columns, where in _INDEXES:
            op.create_index(
                name,
                table,
                columns,
                postgresql_where=sa.text(where),
                postgresql_concurrently=True,
                if_not_exists=True,
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, _columns, _where in reversed(_INDEXES):
            op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
