"""Proxied business system calls: per-grant write permission and a call record.

Additive only: the previous version never reads `allow_write` or `system_calls` during a rolling
upgrade, and `token_delivery` stays `env` until an administrator switches a system to `proxy`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

OLD_PURPOSES = ("chat", "cron", "health", "access_test", "catalog")
PURPOSES = (*OLD_PURPOSES, "proxy")


def _purpose_check(values: tuple[str, ...]) -> str:
    return f"purpose IN ({','.join(repr(p) for p in values)})"


def upgrade():
    op.add_column(
        "bot_system_grants",
        sa.Column("allow_write", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "system_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "called_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("task_id", sa.BigInteger()),
        sa.Column("bot_id", sa.Uuid()),
        sa.Column("user_id", sa.Uuid()),
        sa.Column("system_key", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("risk", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("token_id", sa.Text()),
    )
    op.create_index("system_calls_task_idx", "system_calls", ["task_id"])
    op.create_index("system_calls_time_idx", "system_calls", ["called_at"])
    op.drop_constraint(
        op.f("ck_business_token_issues_purpose"), "business_token_issues", type_="check"
    )
    op.create_check_constraint("purpose", "business_token_issues", _purpose_check(PURPOSES))


def downgrade():
    op.execute("DELETE FROM business_token_issues WHERE purpose = 'proxy'")
    op.drop_constraint(
        op.f("ck_business_token_issues_purpose"), "business_token_issues", type_="check"
    )
    op.create_check_constraint("purpose", "business_token_issues", _purpose_check(OLD_PURPOSES))
    op.drop_table("system_calls")
    op.drop_column("bot_system_grants", "allow_write")
