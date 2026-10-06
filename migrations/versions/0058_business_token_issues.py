"""Record each business-system token issuance so a downstream token id traces back to its turn.

New table only: the previous version never reads or writes it during a rolling upgrade.
"""

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

PURPOSES = ("chat", "cron", "health", "access_test")


def upgrade():
    op.create_table(
        "business_token_issues",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("task_id", sa.BigInteger()),
        sa.Column("bot_id", sa.Uuid()),
        sa.Column("user_id", sa.Uuid()),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("system_key", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("token_id", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"purpose IN ({','.join(repr(p) for p in PURPOSES)})", name="purpose"),
    )
    op.create_index(
        "business_token_issues_token_idx",
        "business_token_issues",
        ["token_id"],
        postgresql_where=sa.text("token_id IS NOT NULL"),
    )
    op.create_index("business_token_issues_task_idx", "business_token_issues", ["task_id"])
    op.create_index("business_token_issues_time_idx", "business_token_issues", ["issued_at"])


def downgrade():
    op.drop_table("business_token_issues")
