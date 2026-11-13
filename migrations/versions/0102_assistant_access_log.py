"""Personal assistant access log: every decryption of an assistant's content outside its own
turns, with who, what and when, and no content."""

import sqlalchemy as sa
from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None

KINDS = "actor_kind IN ('owner', 'admin', 'system')"
FOREIGN = "actor_kind <> 'owner'"


def upgrade() -> None:
    op.create_table(
        "assistant_access_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(KINDS, name="actor_kind"),
    )
    # 所有者翻自己的记录。
    op.create_index("assistant_access_log_bot_at_idx", "assistant_access_log", ["bot_id", "at"])
    # 保留期清理。
    op.create_index("assistant_access_log_at_idx", "assistant_access_log", ["at"])
    # 管理台列表的「最近一次非所有者访问」：这样的行很少，单独一个部分索引。
    op.create_index(
        "assistant_access_log_foreign_idx",
        "assistant_access_log",
        ["bot_id", "at"],
        postgresql_where=sa.text(FOREIGN),
    )


def downgrade() -> None:
    op.drop_index("assistant_access_log_foreign_idx", table_name="assistant_access_log")
    op.drop_index("assistant_access_log_at_idx", table_name="assistant_access_log")
    op.drop_index("assistant_access_log_bot_at_idx", table_name="assistant_access_log")
    op.drop_table("assistant_access_log")
