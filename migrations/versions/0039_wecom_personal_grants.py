"""Private-chat WeCom tools: which member authorized a bot, the tier they chose, and which
chat turns used them."""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

STATUSES = ("selecting", "connected", "revoked")
LEVELS = ("readonly", "all_except_send", "all")


def _check(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade():
    # Turns that mounted a member's WeCom tools may hold their mail or documents: owner only.
    op.add_column(
        "chat_logs",
        sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "wecom_personal_grants",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("authorization_level", sa.Text(), nullable=False),
        sa.Column(
            "context_epoch", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("authorizer_id", sa.Text()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("token_enc", sa.Text()),
        sa.Column("bot_fingerprint", sa.Text(), nullable=False, server_default=""),
        sa.Column("selection_chat_id", sa.Text()),
        sa.Column("selection_task_id", sa.BigInteger()),
        sa.Column("selection_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_check("status", STATUSES), name="status"),
        sa.CheckConstraint(_check("authorization_level", LEVELS), name="authorization_level"),
    )
    op.create_index("ix_wecom_personal_grants_user_id", "wecom_personal_grants", ["user_id"])
    op.execute(
        "CREATE TRIGGER trg_wecom_personal_grants_updated_at BEFORE UPDATE ON "
        "wecom_personal_grants FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade():
    op.drop_table("wecom_personal_grants")
    op.drop_column("chat_logs", "private")
