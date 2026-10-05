"""One-time credential handoff: a request can ask for values the agent uses once and drops.

- `save`: whether submitted values go to `personal_credentials`. Existing requests were all saved,
  and the previous version inserts without the column during a rolling upgrade, so it defaults
  to true.
- `handoff_enc`: the encrypted values of a one-time request, held only until its resume turn ends.
"""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "credential_requests",
        sa.Column("save", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column("credential_requests", sa.Column("handoff_enc", sa.Text()))
    op.create_index(
        "credential_requests_handoff_idx",
        "credential_requests",
        ["submitted_at"],
        postgresql_where=sa.text("handoff_enc IS NOT NULL"),
    )


def downgrade():
    op.drop_index("credential_requests_handoff_idx", table_name="credential_requests")
    op.drop_column("credential_requests", "handoff_enc")
    op.drop_column("credential_requests", "save")
