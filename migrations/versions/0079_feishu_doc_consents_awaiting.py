"""Group document reads wait for the asker's Feishu authorization and resume afterwards."""

from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None

_OLD = "status IN ('pending', 'approved', 'declined', 'expired')"
_NEW = "status IN ('pending', 'approved', 'declined', 'expired', 'awaiting_auth', 'resumed')"


def upgrade() -> None:
    op.drop_constraint("status", "feishu_doc_consents", type_="check")
    op.create_check_constraint("status", "feishu_doc_consents", _NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE feishu_doc_consents SET status = 'expired'"
        " WHERE status IN ('awaiting_auth', 'resumed')"
    )
    op.drop_constraint("status", "feishu_doc_consents", type_="check")
    op.create_check_constraint("status", "feishu_doc_consents", _OLD)
