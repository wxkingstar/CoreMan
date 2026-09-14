"""让应用通知复用 outbox，普通机器人消息仍必须绑定 bot。"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("outbox", "bot_id", existing_type=sa.Uuid(), nullable=True)
    op.drop_constraint(op.f("ck_outbox_kind"), "outbox", type_="check")
    op.create_check_constraint(
        "kind", "outbox", "kind IN ('send','card_update','welcome','stream_finish','notify')"
    )
    op.create_check_constraint(
        "target_kind",
        "outbox",
        "(kind = 'notify' AND bot_id IS NULL) OR (kind != 'notify' AND bot_id IS NOT NULL)",
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM outbox WHERE kind='notify')"))
        .scalar()
    ):
        raise RuntimeError("通知记录仍在 outbox，不能无损回退；请先归档这些记录")
    op.drop_constraint(op.f("ck_outbox_target_kind"), "outbox", type_="check")
    op.drop_constraint(op.f("ck_outbox_kind"), "outbox", type_="check")
    op.create_check_constraint(
        "kind", "outbox", "kind IN ('send','card_update','welcome','stream_finish')"
    )
    op.alter_column("outbox", "bot_id", existing_type=sa.Uuid(), nullable=False)
