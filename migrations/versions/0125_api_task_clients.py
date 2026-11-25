"""API tasks started by signed API clients on behalf of a member.

`api_clients.bot_ids` limits which bots a client with the `tasks` scope may drive.
`api_tasks` rows now come from either a personal token (`token_id`) or a client (`client_key`).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "api_clients",
        sa.Column(
            "bot_ids",
            postgresql.ARRAY(sa.Uuid()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.alter_column("api_tasks", "token_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("api_tasks", sa.Column("client_key", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_api_tasks_client_key_api_clients",
        "api_tasks",
        "api_clients",
        ["client_key"],
        ["app_key"],
        ondelete="CASCADE",
    )
    op.create_check_constraint("caller", "api_tasks", "(token_id IS NULL) <> (client_key IS NULL)")
    op.create_unique_constraint(
        "uq_api_tasks_client_request", "api_tasks", ["client_key", "request_id"]
    )
    op.create_index("api_tasks_client_idx", "api_tasks", ["client_key", "user_id", "created_at"])


def downgrade() -> None:
    op.execute("DELETE FROM api_tasks WHERE client_key IS NOT NULL")
    op.drop_index("api_tasks_client_idx", table_name="api_tasks")
    op.drop_constraint("uq_api_tasks_client_request", "api_tasks", type_="unique")
    op.drop_constraint("caller", "api_tasks", type_="check")
    op.drop_constraint("fk_api_tasks_client_key_api_clients", "api_tasks", type_="foreignkey")
    op.drop_column("api_tasks", "client_key")
    op.alter_column("api_tasks", "token_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("api_clients", "bot_ids")
