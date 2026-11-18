"""Approvals linked to follow-ups; owner-confirmed business-system access requests."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None

STATUSES = "status IN ('pending', 'approved', 'rejected', 'withdrawn')"
APPROVAL_FK = "fk_assistant_approvals_followup_id_assistant_followups"


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("assistant_approvals", sa.Column("followup_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        APPROVAL_FK,
        "assistant_approvals",
        "assistant_followups",
        ["followup_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "system_access_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "gap_id",
            sa.Uuid(),
            sa.ForeignKey("assistant_gaps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "system_key",
            sa.Text(),
            sa.ForeignKey("systems.key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("write", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("decided_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        _ts("decided_at", nullable=True),
        sa.CheckConstraint(STATUSES, name="status"),
    )
    op.create_index(
        "system_access_requests_pending_uq",
        "system_access_requests",
        ["bot_id", "system_key"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "system_access_requests_status_idx", "system_access_requests", ["status", "created_at"]
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("system_access_requests_status_idx", table_name="system_access_requests")
    op.drop_index("system_access_requests_pending_uq", table_name="system_access_requests")
    op.drop_table("system_access_requests")
    op.drop_constraint(APPROVAL_FK, "assistant_approvals", type_="foreignkey")
    op.drop_column("assistant_approvals", "followup_id")
