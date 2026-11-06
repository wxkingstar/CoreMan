"""Group chats read Feishu documents: per-document consent from the person who asked."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feishu_doc_consents",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("tokens", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("urls", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("share_offered", sa.Boolean(), nullable=False),
        sa.Column("shared", sa.Boolean(), nullable=False),
        sa.Column("origin_task_id", sa.BigInteger(), nullable=True),
        sa.Column("card_outbox_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'declined', 'expired')", name="status"
        ),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["origin_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "feishu_doc_consents_lookup_idx",
        "feishu_doc_consents",
        ["bot_id", "chat_id", "user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("feishu_doc_consents_lookup_idx", table_name="feishu_doc_consents")
    op.drop_table("feishu_doc_consents")
