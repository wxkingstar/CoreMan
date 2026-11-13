"""Personal assistant settings: owner identity for platform-origin turns, hours, watch lists."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

HHMM = "'^([01][0-9]|2[0-3]):[0-5][0-9]$'"
# 已开通的助理：身份取自最近一条完成的开通流程（点开通时飞书给的操作人、开通卡片所在的私聊）。
# 没有这样一条流程的记录（本功能上线前建的）不建行，平台来源的轮次对它们不可用。
BACKFILL = """
INSERT INTO assistant_settings (bot_id, open_id, tenant_key, platform_user_id, chat_id)
SELECT DISTINCT ON (flow.bot_id)
       flow.bot_id, flow.open_id, flow.tenant_key, flow.platform_user_id, flow.offer_chat_id
FROM assistant_lifecycles flow
JOIN bots ON bots.id = flow.bot_id AND bots.kind = 'assistant' AND bots.owner_id = flow.owner_id
WHERE flow.action = 'provision' AND flow.outcome = 'done'
  AND flow.open_id IS NOT NULL AND flow.tenant_key IS NOT NULL
  AND flow.offer_chat_id IS NOT NULL AND flow.platform_user_id <> ''
ORDER BY flow.bot_id, flow.finished_at DESC
"""


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _text(name: str, default: str) -> sa.Column:
    return sa.Column(name, sa.Text(), server_default=sa.text(f"'{default}'"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "assistant_settings",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
        ),
        _text("open_id", ""),
        _text("tenant_key", ""),
        _text("platform_user_id", ""),
        _text("chat_id", ""),
        _text("timezone", "Asia/Shanghai"),
        _text("work_start", "09:30"),
        _text("work_end", "19:00"),
        sa.Column(
            "workdays",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default=sa.text("'{1,2,3,4,5}'::smallint[]"),
            nullable=False,
        ),
        sa.Column("focus_chats_enc", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ignored_chats_enc", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("daily_turn_budget", sa.Integer(), nullable=True),
        sa.Column("shift_override", sa.Text(), nullable=True),
        _ts("shift_override_until", nullable=True),
        _ts("cold_start_done_at", nullable=True),
        _ts("updated_at", server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            f"work_start ~ {HHMM} AND work_end ~ {HHMM} AND work_start < work_end",
            name="work_hours",
        ),
        sa.CheckConstraint("workdays <@ ARRAY[1,2,3,4,5,6,7]::smallint[]", name="workdays"),
        sa.CheckConstraint(
            "shift_override IS NULL OR shift_override IN ('on', 'off')", name="shift_override"
        ),
        sa.CheckConstraint(
            "daily_turn_budget IS NULL OR daily_turn_budget BETWEEN 10 AND 2000",
            name="daily_turn_budget",
        ),
    )
    op.execute(BACKFILL)


def downgrade() -> None:
    op.drop_table("assistant_settings")
