"""Drive AI employees through the API: personal access tokens, API tasks that run in a Feishu
conversation, group aliases that name those conversations, and a per-bot opt-in switch."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.add_column(
        "bots",
        sa.Column("api_access", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_prefix", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("bot_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "personal_access_tokens_user_idx", "personal_access_tokens", ["user_id", "created_at"]
    )
    op.create_table(
        "api_tasks",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=True),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.CheckConstraint("target IN ('p2p', 'chat')", name="target"),
        sa.ForeignKeyConstraint(["token_id"], ["personal_access_tokens.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_id", "request_id", name="uq_api_tasks_token_request"),
    )
    op.create_index("api_tasks_token_idx", "api_tasks", ["token_id", "created_at"])
    op.create_table(
        "bot_conversation_aliases",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "alias", name="uq_bot_conversation_aliases_alias"),
        sa.UniqueConstraint("bot_id", "chat_id", name="uq_bot_conversation_aliases_chat"),
    )


def downgrade() -> None:
    op.drop_table("bot_conversation_aliases")
    op.drop_index("api_tasks_token_idx", table_name="api_tasks")
    op.drop_table("api_tasks")
    op.drop_index("personal_access_tokens_user_idx", table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")
    op.drop_column("bots", "api_access")
