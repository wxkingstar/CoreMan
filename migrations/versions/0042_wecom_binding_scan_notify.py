"""Remember which private chat asked to bind WeCom, so the result can be announced there."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("wecom_personal_bindings", sa.Column("scan_notify", JSONB()))


def downgrade():
    op.drop_column("wecom_personal_bindings", "scan_notify")
