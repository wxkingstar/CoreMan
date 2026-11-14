"""Global skills: a catalog skill marked global is installed on every standard AI employee
(global_skills); bot_skills.installed_globally marks rows installed because of it."""

import sqlalchemy as sa
from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "global_skills",
        sa.Column(
            "skill_id",
            sa.Uuid(),
            sa.ForeignKey("skills.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("set_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.add_column(
        "bot_skills",
        sa.Column(
            "installed_globally", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("bot_skills", "installed_globally")
    op.drop_table("global_skills")
