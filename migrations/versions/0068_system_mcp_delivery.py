"""Business systems served as an MCP server, with an optional companion skill.

Additive only: `token_delivery` stays as it is until an administrator switches a system to `mcp`,
and the previous version never reads the new columns during a rolling upgrade.
"""

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("systems", sa.Column("mcp_url", sa.Text(), nullable=True))
    op.add_column(
        "systems",
        sa.Column(
            "companion_skill_id",
            sa.Uuid(),
            sa.ForeignKey("skills.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.drop_constraint(op.f("ck_systems_token_delivery"), "systems", type_="check")
    op.create_check_constraint(
        "token_delivery", "systems", "token_delivery IN ('env','proxy','mcp')"
    )
    op.add_column("bot_skills", sa.Column("installed_by_system", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("bot_skills", "installed_by_system")
    # 旧版本不认识 mcp：降级时停用这些系统，而不是把令牌改成下发到运行环境。
    op.execute(
        "UPDATE systems SET token_delivery = 'env', enabled = false WHERE token_delivery = 'mcp'"
    )
    op.drop_constraint(op.f("ck_systems_token_delivery"), "systems", type_="check")
    op.create_check_constraint("token_delivery", "systems", "token_delivery IN ('env','proxy')")
    op.drop_column("systems", "companion_skill_id")
    op.drop_column("systems", "mcp_url")
