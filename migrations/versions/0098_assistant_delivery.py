"""Personal assistant delivery: per-assistant state, daily sessions, inbox retries, grant date,
and the ids of messages the platform sent as the owner."""

import sqlalchemy as sa
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    default = None if nullable else sa.text("now()")
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "assistant_delivery",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("handover_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("budget_notified_on", sa.Date(), nullable=True),
        sa.Column("reauth_reminded_on", sa.Date(), nullable=True),
        _ts("next_sweep_at"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("assistant_delivery_sweep_idx", "assistant_delivery", ["next_sweep_at"])
    # 平台经审批以所有者身份发出的消息的编号：盯消息读到它们时不当作所有者亲自发言。
    op.create_table(
        "assistant_sent",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("message_id", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("bot_id", "message_id"),
    )
    op.add_column("chat_sessions", sa.Column("local_day", sa.Date(), nullable=True))
    op.add_column(
        "assistant_inbox",
        sa.Column("failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index(
        "assistant_inbox_in_flight_idx",
        "assistant_inbox",
        ["delivered_task_id"],
        postgresql_where=sa.text("delivered_task_id IS NOT NULL AND status = 'pending'"),
    )
    op.add_column(
        "feishu_personal_grants",
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 已经连接的授权不知道是哪天完成的：按最早的可能时间算，宁可早提醒。
    op.execute(
        "UPDATE feishu_personal_grants SET authorized_at = created_at WHERE status = 'connected'"
    )


def downgrade() -> None:
    op.drop_column("feishu_personal_grants", "authorized_at")
    op.drop_index("assistant_inbox_in_flight_idx", table_name="assistant_inbox")
    op.drop_column("assistant_inbox", "failures")
    op.drop_column("chat_sessions", "local_day")
    op.drop_table("assistant_sent")
    op.drop_index("assistant_delivery_sweep_idx", table_name="assistant_delivery")
    op.drop_table("assistant_delivery")
