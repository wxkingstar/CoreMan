"""Personal assistant knowledge base: encrypted pages addressed by a keyed path digest."""

import sqlalchemy as sa
from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_pages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("path_hmac", sa.Text(), nullable=False),
        sa.Column("path_enc", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("title_enc", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("summary_enc", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("body_enc", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("size", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by", sa.Text(), server_default=sa.text("'assistant'"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("updated_by IN ('owner', 'assistant', 'import')", name="updated_by"),
        sa.UniqueConstraint("bot_id", "path_hmac", name="uq_assistant_pages_path"),
    )


def downgrade() -> None:
    op.drop_table("assistant_pages")
