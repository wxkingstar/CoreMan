"""Mail sent as a member is deduplicated by the caller's uuid; Feishu mail has no idempotency key.

A new table only: the previous version never reads or writes it during a rolling upgrade.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

STATUSES = ("drafted", "sent")


def _check(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade():
    op.create_table(
        "feishu_personal_sends",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("send_key", sa.Text(), primary_key=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("draft_id", sa.Text()),
        sa.Column("result", JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_check("status", STATUSES), name="status"),
    )
    op.create_index("feishu_personal_sends_created_idx", "feishu_personal_sends", ["created_at"])
    op.execute(
        "CREATE TRIGGER trg_feishu_personal_sends_updated_at BEFORE UPDATE ON "
        "feishu_personal_sends FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade():
    op.drop_table("feishu_personal_sends")
