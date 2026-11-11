"""feishu_deliveries.bot_id: lets assistant card layouts be sealed like other content rows."""

import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feishu_deliveries",
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_feishu_deliveries_bot_id_bots"),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE feishu_deliveries d SET bot_id = s.bot_id "
        "FROM task_streams s WHERE s.task_id = d.task_id"
    )


def downgrade() -> None:
    op.drop_column("feishu_deliveries", "bot_id")
