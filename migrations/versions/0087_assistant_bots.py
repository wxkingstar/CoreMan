"""Personal assistant records: bots.kind, owner_id, platform_app_id."""

import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

SHAPE = (
    "(kind = 'standard' AND owner_id IS NULL AND platform_app_id IS NULL) OR "
    "(kind = 'assistant' AND owner_id IS NOT NULL AND platform_app_id IS NOT NULL "
    "AND platform = 'feishu')"
)


def upgrade() -> None:
    # 改的是在用的表，要拿排他锁：排在长事务后面会让之后对这张表的读写全部跟着排队。
    # 5 秒拿不到锁就报错，整次迁移回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "bots",
        sa.Column("kind", sa.Text(), server_default=sa.text("'standard'"), nullable=False),
    )
    op.add_column("bots", sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id")))
    op.add_column(
        "bots", sa.Column("platform_app_id", sa.Uuid(), sa.ForeignKey("platform_apps.id"))
    )
    op.create_check_constraint("kind", "bots", "kind IN ('standard','assistant')")
    op.create_check_constraint("assistant_shape", "bots", SHAPE)
    op.create_index("bots_platform_app_idx", "bots", ["platform_app_id"])
    op.create_index(
        "bots_assistant_owner_uq",
        "bots",
        ["owner_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'assistant' AND enabled"),
    )


def downgrade() -> None:
    # 改的是在用的表，要拿排他锁：排在长事务后面会让之后对这张表的读写全部跟着排队。
    # 5 秒拿不到锁就报错，整次迁移回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM bots WHERE kind = 'assistant'")
    op.drop_index("bots_assistant_owner_uq", table_name="bots")
    op.drop_index("bots_platform_app_idx", table_name="bots")
    op.drop_constraint(op.f("ck_bots_assistant_shape"), "bots", type_="check")
    op.drop_constraint(op.f("ck_bots_kind"), "bots", type_="check")
    op.drop_column("bots", "platform_app_id")
    op.drop_column("bots", "owner_id")
    op.drop_column("bots", "kind")
