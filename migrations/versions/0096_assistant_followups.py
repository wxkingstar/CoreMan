"""Personal assistant follow-ups: numbered items with plaintext category, status and dates."""

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None

CATEGORIES = "category IN ('decide', 'promised', 'waiting', 'remind')"
STATUSES = "status IN ('open', 'snoozed', 'done', 'dropped')"
CLOSE_CODES = "close_code IS NULL OR close_code IN ('origin_recalled')"


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "assistant_followups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("review_on", sa.Date(), nullable=True),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("origin_message_id", sa.Text(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("title_enc", sa.Text(), nullable=False),
        sa.Column("notes_enc", sa.Text(), nullable=True),
        sa.Column("close_code", sa.Text(), nullable=True),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        _ts("updated_at", server_default=sa.text("now()"), nullable=False),
        _ts("closed_at", nullable=True),
        sa.CheckConstraint(CATEGORIES, name="category"),
        sa.CheckConstraint(STATUSES, name="status"),
        sa.CheckConstraint(CLOSE_CODES, name="close_code"),
        sa.UniqueConstraint("bot_id", "seq", name="uq_assistant_followups_bot_seq"),
    )
    op.create_index(
        "assistant_followups_bot_status_idx", "assistant_followups", ["bot_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("assistant_followups_bot_status_idx", table_name="assistant_followups")
    op.drop_table("assistant_followups")
