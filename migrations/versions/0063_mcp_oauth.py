"""MCP skills that need an OAuth login, authorized per AI employee on its runtime instance.

- `skills.mcp_oauth`: the MCP server is a remote one whose CLI login (Codex / Claude Code
  `mcp login`) holds the token; installation names it per AI employee.
- `bot_skills.mcp_authorized_*`: who authorized it, when, and on which runtime instance. The
  token itself stays in that instance's CLI credential store; moving the employee to another
  instance needs a new authorization.
"""

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "skills",
        sa.Column("mcp_oauth", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("bot_skills", sa.Column("mcp_authorized_at", sa.DateTime(timezone=True)))
    op.add_column("bot_skills", sa.Column("mcp_authorized_by", sa.Uuid()))
    op.add_column("bot_skills", sa.Column("mcp_authorized_relay_id", sa.Uuid()))


def downgrade():
    op.drop_column("bot_skills", "mcp_authorized_relay_id")
    op.drop_column("bot_skills", "mcp_authorized_by")
    op.drop_column("bot_skills", "mcp_authorized_at")
    op.drop_column("skills", "mcp_oauth")
