"""Follow-ups: who is handling it, and when it last made progress."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None

HANDLERS = ("assistant", "job", "person", "owner")


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "assistant_followups",
        sa.Column("handler", sa.Text(), nullable=False, server_default=sa.text("'assistant'")),
    )
    op.add_column("assistant_followups", sa.Column("handler_name_enc", sa.Text(), nullable=True))
    op.add_column(
        "assistant_followups",
        sa.Column(
            "progress_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # 老条目的进展时间取它最后一次被改的时刻，免得上线当天全都算「刚有进展」。
    op.execute("UPDATE assistant_followups SET progress_at = updated_at")
    # 挂着子任务的老条目就是子任务在办。
    op.execute("UPDATE assistant_followups SET handler = 'job' WHERE job_id IS NOT NULL")
    values = ", ".join(f"'{v}'" for v in HANDLERS)
    op.create_check_constraint(
        "ck_assistant_followups_handler", "assistant_followups", f"handler IN ({values})"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_assistant_followups_handler", "assistant_followups", type_="check")
    op.drop_column("assistant_followups", "progress_at")
    op.drop_column("assistant_followups", "handler_name_enc")
    op.drop_column("assistant_followups", "handler")
