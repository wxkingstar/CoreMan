"""Git username sent with a skill source's access token; NULL keeps oauth2."""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skill_sources", sa.Column("git_username", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("skill_sources", "git_username")
