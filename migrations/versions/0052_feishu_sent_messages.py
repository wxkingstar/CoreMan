"""Text of every Feishu message a bot sends, so a later quote of it can be read back.

Streamed reply state is dropped an hour after a reply ends, and Feishu's message API does not
return the body of Card JSON 2.0 cards by default. A new table only: the previous version never
reads or writes it during a rolling upgrade.
"""

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feishu_sent_messages",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("message_id", sa.Text(), primary_key=True),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("feishu_sent_messages_created_idx", "feishu_sent_messages", ["created_at"])


def downgrade():
    op.drop_table("feishu_sent_messages")
