"""Group delivery for API clients: `api_client_group_routes`.

A row lets a client start turns for an AI employee in one group's main session, as a fixed service
account. Additive only: the previous version never reads the new table during a rolling upgrade.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "api_client_group_routes",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("client_key", sa.Text(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("service_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["client_key"], ["api_clients.app_key"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_key", "bot_id", "chat_id", name="uq_api_client_group_routes_chat"
        ),
    )


def downgrade() -> None:
    op.drop_table("api_client_group_routes")
