"""Follow-up states: why a follow-up is stuck, nudge count, owner close reasons, owner feedback."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None

OLD_CLOSE = "close_code IS NULL OR close_code IN ('origin_recalled')"
NEW_CLOSE = (
    "close_code IS NULL OR close_code IN "
    "('origin_recalled', 'talked', 'resolved', 'others_done', 'not_needed', 'other')"
)
STUCK = "stuck IS NULL OR stuck IN ('how', 'who', 'failed')"
KINDS = "kind IN ('supplement', 'reschedule', 'close')"


def upgrade() -> None:
    # 待跟进是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("assistant_followups", sa.Column("stuck", sa.Text(), nullable=True))
    op.add_column(
        "assistant_followups",
        sa.Column("nudges", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint("stuck", "assistant_followups", STUCK)
    op.drop_constraint(
        op.f("ck_assistant_followups_close_code"), "assistant_followups", type_="check"
    )
    op.create_check_constraint("close_code", "assistant_followups", NEW_CLOSE)
    op.create_table(
        "assistant_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "followup_id",
            sa.Uuid(),
            sa.ForeignKey("assistant_followups.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("seq", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("note_enc", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(KINDS, name="kind"),
    )
    op.create_index(
        "assistant_feedback_bot_created_idx", "assistant_feedback", ["bot_id", "created_at"]
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("assistant_feedback_bot_created_idx", table_name="assistant_feedback")
    op.drop_table("assistant_feedback")
    op.execute(
        "UPDATE assistant_followups SET close_code = NULL WHERE close_code <> 'origin_recalled'"
    )
    op.drop_constraint(
        op.f("ck_assistant_followups_close_code"), "assistant_followups", type_="check"
    )
    op.create_check_constraint("close_code", "assistant_followups", OLD_CLOSE)
    op.drop_constraint(op.f("ck_assistant_followups_stuck"), "assistant_followups", type_="check")
    op.drop_column("assistant_followups", "nudges")
    op.drop_column("assistant_followups", "stuck")
