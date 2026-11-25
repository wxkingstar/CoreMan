"""Members' own agents call proxied business systems through the catalog MCP.

Calls and token issues made for a member's OAuth grant (no task) carry `oauth_grant_id`; a write
waits for the member's confirmation as an interaction state of kind `member_write`. Additive only:
the previous version never reads the new columns or kind during a rolling upgrade.
"""

import sqlalchemy as sa
from alembic import op

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None

KINDS_BEFORE = (
    "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
    " 'personal_schedule', 'feishu_group_write', 'systems_write', 'cloud_write',"
    " 'assistant_write', 'systems_batch')"
)
KINDS_AFTER = (
    "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
    " 'personal_schedule', 'feishu_group_write', 'systems_write', 'cloud_write',"
    " 'assistant_write', 'systems_batch', 'member_write')"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("system_calls", sa.Column("oauth_grant_id", sa.Uuid(), nullable=True))
    op.create_index("system_calls_user_time_idx", "system_calls", ["user_id", "called_at"])
    op.add_column("business_token_issues", sa.Column("oauth_grant_id", sa.Uuid(), nullable=True))
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint("kind", "interaction_states", KINDS_AFTER)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM interaction_states WHERE kind = 'member_write'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint("kind", "interaction_states", KINDS_BEFORE)
    op.drop_column("business_token_issues", "oauth_grant_id")
    op.drop_index("system_calls_user_time_idx", table_name="system_calls")
    op.drop_column("system_calls", "oauth_grant_id")
