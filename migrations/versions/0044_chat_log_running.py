"""chat_logs 在开流时写入进行中的一行，收尾时改成终态。

状态新增 `running`，只是放宽检查约束，上一版本照常写终态。换约束要短暂拿表的排他锁：设了
lock_timeout，排不上就报错重试，不在锁队列里挡住对话写记录。新约束先以 NOT VALID 加上（只改
元数据），再在事务外校验、并发建索引。
"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

STATUSES = "'running','success','error','timeout','stopped','ask_user','failed'"
OLD_STATUSES = "'success','error','timeout','stopped','ask_user','failed'"
INDEXES = {
    "chat_logs_task_idx": "CREATE INDEX CONCURRENTLY chat_logs_task_idx ON chat_logs (task_id)",
    "chat_logs_running_task_uk": (
        "CREATE UNIQUE INDEX CONCURRENTLY chat_logs_running_task_uk "
        "ON chat_logs (task_id) WHERE status = 'running'"
    ),
}


def _index_state(name: str) -> bool | None:
    """索引是否可用；不存在为 None。并发建索引中断会留下一个不可用的索引。"""
    return op.get_bind().scalar(
        sa.text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name"
        ),
        {"name": name},
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(op.f("ck_chat_logs_status"), "chat_logs", type_="check")
    op.execute(
        f"ALTER TABLE chat_logs ADD CONSTRAINT ck_chat_logs_status "
        f"CHECK (status IN ({STATUSES})) NOT VALID"
    )
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE chat_logs VALIDATE CONSTRAINT ck_chat_logs_status")
        for name, ddl in INDEXES.items():
            state = _index_state(name)
            if state is False:
                # 上次并发建到一半：这个索引不可用，ON CONFLICT 也认不出它，删掉重建。
                op.execute(f"DROP INDEX CONCURRENTLY {name}")
            if state is not True:
                op.execute(ddl)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS chat_logs_running_task_uk")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS chat_logs_task_idx")
    # 先挡住写入再改：否则改完之后又插进来的进行中记录会让旧约束加不上。
    op.execute("LOCK TABLE chat_logs IN ACCESS EXCLUSIVE MODE")
    # 旧约束不认 running：还没收尾的行按中断记。
    op.execute(
        "UPDATE chat_logs SET status = 'error', "
        "error_code = coalesce(error_code, 'turn_unfinished') WHERE status = 'running'"
    )
    op.drop_constraint(op.f("ck_chat_logs_status"), "chat_logs", type_="check")
    op.create_check_constraint("status", "chat_logs", f"status IN ({OLD_STATUSES})")
