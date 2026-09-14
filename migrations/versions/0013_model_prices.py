"""按生效日维护模型价格，不填充猜测价格。"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_prices",
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column("model", sa.Text(), primary_key=True),
        sa.Column("effective_from", sa.Date(), primary_key=True),
        sa.Column("input_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("output_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("cache_read_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("cache_write_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint(
            "input_usd >= 0 AND output_usd >= 0 AND cache_read_usd >= 0 AND cache_write_usd >= 0",
            name=op.f("ck_model_prices_nonnegative"),
        ),
    )


def downgrade() -> None:
    op.drop_table("model_prices")
