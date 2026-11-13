"""Personal assistant cold start and end-of-day compile: progress columns on assistant_settings.

已开通的助理不回填：他们下一次来私聊时才开始冷启动（见 2F 计划「设计选择」第 13 条）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

TABLE = "assistant_settings"
STEPS = (
    "cold_start_step IS NULL OR cold_start_step IN ('voice', 'people', 'duties', 'focus', 'hours')"
)
OUTCOMES = "cold_start_outcome IS NULL OR cold_start_outcome IN ('completed', 'skipped', 'timeout')"
INDEX = "assistant_settings_cold_start_idx"


def upgrade() -> None:
    op.add_column(
        TABLE, sa.Column("cold_start_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(TABLE, sa.Column("cold_start_step", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("cold_start_next_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("cold_start_outcome", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("worked_on", sa.Date(), nullable=True))
    op.add_column(TABLE, sa.Column("compiled_on", sa.Date(), nullable=True))
    op.add_column(TABLE, sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("cold_start_step", TABLE, STEPS)
    op.create_check_constraint("cold_start_outcome", TABLE, OUTCOMES)
    op.create_index(
        INDEX,
        TABLE,
        ["cold_start_started_at"],
        postgresql_where=sa.text(
            "cold_start_done_at IS NULL AND cold_start_started_at IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_constraint("cold_start_outcome", TABLE, type_="check")
    op.drop_constraint("cold_start_step", TABLE, type_="check")
    for name in (
        "compiled_at",
        "compiled_on",
        "worked_on",
        "cold_start_outcome",
        "cold_start_next_at",
        "cold_start_step",
        "cold_start_started_at",
    ):
        op.drop_column(TABLE, name)
