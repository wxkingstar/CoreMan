"""Git backup can take its access token from an environment preset tagged `git`.

- `git_token_preset`: the preset's group key; NULL means the bot's own `git_token_enc` is used.
- `git_token_var`: which variable of that preset holds the token.

Presets are never deleted, so no foreign key; a missing preset or tag fails at use time.
"""

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bots", sa.Column("git_token_preset", sa.Text()))
    op.add_column("bots", sa.Column("git_token_var", sa.Text()))


def downgrade():
    op.drop_column("bots", "git_token_var")
    op.drop_column("bots", "git_token_preset")
