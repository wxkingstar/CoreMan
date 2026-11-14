"""Personal assistants' low-risk cloud proxy writes wait for the owner's confirmation card
(interaction kind cloud_write); cloud_calls records the staged call (proposed) and the outcome of
a confirmed run (confirmed_*)."""

from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None

KINDS_BEFORE = (
    "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
    " 'personal_schedule', 'feishu_group_write', 'systems_write')"
)
KINDS_AFTER = (
    "kind IN ('choice', 'relay_switch', 'session_switch', 'self_reminder',"
    " 'personal_schedule', 'feishu_group_write', 'systems_write', 'cloud_write')"
)
OUTCOMES_BEFORE = "outcome IN ('ok', 'http_error', 'denied', 'approval_required', 'unreachable')"
OUTCOMES_AFTER = (
    "outcome IN ('ok', 'http_error', 'denied', 'approval_required', 'unreachable', 'proposed',"
    " 'confirmed_ok', 'confirmed_http_error', 'confirmed_denied', 'confirmed_unreachable')"
)


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint("kind", "interaction_states", KINDS_AFTER)
    op.drop_constraint("outcome", "cloud_calls", type_="check")
    op.create_check_constraint("outcome", "cloud_calls", OUTCOMES_AFTER)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 审计行不删：新结局折回旧取值（暂存记作等审批，确认后执行的去掉前缀）。
    op.execute(
        "UPDATE cloud_calls SET outcome = CASE outcome WHEN 'proposed' THEN 'approval_required'"
        " ELSE substr(outcome, length('confirmed_') + 1) END"
        " WHERE outcome = 'proposed' OR outcome LIKE 'confirmed\\_%'"
    )
    op.drop_constraint("outcome", "cloud_calls", type_="check")
    op.create_check_constraint("outcome", "cloud_calls", OUTCOMES_BEFORE)
    op.execute("DELETE FROM interaction_states WHERE kind = 'cloud_write'")
    op.drop_constraint("kind", "interaction_states", type_="check")
    op.create_check_constraint("kind", "interaction_states", KINDS_BEFORE)
