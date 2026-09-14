"""求助调用方归属与同一接收人单活跃约束。"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0008 只有模型，尚未开放求助入口，不应存在无法证明调用方的记录。
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM escalations)")).scalar():
        raise RuntimeError("存在未归属的求助记录，请先核实调用方，不能推断其身份")
    op.add_column("escalations", sa.Column("owner_client_key", sa.Text(), nullable=False))
    op.add_column("escalations", sa.Column("request_fingerprint", sa.Text(), nullable=False))
    op.add_column("escalations", sa.Column("from_platform_user_id", sa.Text(), nullable=True))
    op.add_column("escalations", sa.Column("to_platform_user_id", sa.Text(), nullable=False))
    op.add_column(
        "escalations",
        sa.Column(
            "followup_questions",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_foreign_key(
        op.f("fk_escalations_owner_client_key_api_clients"),
        "escalations",
        "api_clients",
        ["owner_client_key"],
        ["app_key"],
    )
    op.create_index("escalations_owner_group_idx", "escalations", ["owner_client_key", "group_id"])
    op.create_index(
        "escalations_one_active_recipient",
        "escalations",
        ["to_user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','replied')"),
    )


def downgrade() -> None:
    op.drop_index("escalations_one_active_recipient", table_name="escalations")
    op.drop_index("escalations_owner_group_idx", table_name="escalations")
    op.drop_constraint(
        op.f("fk_escalations_owner_client_key_api_clients"), "escalations", type_="foreignkey"
    )
    op.drop_column("escalations", "followup_questions")
    op.drop_column("escalations", "to_platform_user_id")
    op.drop_column("escalations", "from_platform_user_id")
    op.drop_column("escalations", "request_fingerprint")
    op.drop_column("escalations", "owner_client_key")
