"""Personal assistant capability gaps, and which gap a follow-up is stuck on."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None

KINDS = ("feishu_scope", "business_system", "cloud_product", "credential", "node_tool", "other")
STATUSES = ("open", "requested", "resolved", "declined")
FOLLOWUP_FK = "fk_assistant_followups_gap_id_assistant_gaps"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    # 待跟进是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "assistant_gaps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("need_enc", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        # 报缺口时这项能力已有的级别（0 没有、1 能读、2 能写）：之后升上去才算开通。
        sa.Column("seen_rank", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        _ts("created_at", server_default=sa.text("now()"), nullable=False),
        _ts("updated_at", server_default=sa.text("now()"), nullable=False),
        _ts("resolved_at", nullable=True),
        _ts("declined_at", nullable=True),
        sa.CheckConstraint(_in("kind", KINDS), name="kind"),
        sa.CheckConstraint(_in("status", STATUSES), name="status"),
        sa.UniqueConstraint("bot_id", "kind", "key", name="uq_assistant_gaps_bot_kind_key"),
        sa.UniqueConstraint("bot_id", "seq", name="uq_assistant_gaps_bot_seq"),
    )
    op.create_index("assistant_gaps_bot_status_idx", "assistant_gaps", ["bot_id", "status"])
    op.add_column("assistant_followups", sa.Column("gap_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        FOLLOWUP_FK,
        "assistant_followups",
        "assistant_gaps",
        ["gap_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(FOLLOWUP_FK, "assistant_followups", type_="foreignkey")
    op.drop_column("assistant_followups", "gap_id")
    op.drop_index("assistant_gaps_bot_status_idx", table_name="assistant_gaps")
    op.drop_table("assistant_gaps")
