"""Persist employee workspace operation and Git configuration."""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bots", sa.Column("workspace_state", sa.Text(), nullable=False, server_default="ready")
    )
    op.add_column("bots", sa.Column("workspace_phase", sa.Text()))
    op.add_column("bots", sa.Column("workspace_deadline", sa.DateTime(timezone=True)))
    op.add_column("bots", sa.Column("memory_snapshot_at", sa.DateTime(timezone=True)))
    op.add_column("bots", sa.Column("workspace_error", sa.Text()))
    op.add_column("bots", sa.Column("workspace_operation_id", sa.Uuid()))
    op.add_column("bots", sa.Column("workspace_target_relay_id", sa.Uuid()))
    op.add_column("bots", sa.Column("workspace_target_dir", sa.Text()))
    op.add_column("bots", sa.Column("git_url", sa.Text()))
    op.add_column("bots", sa.Column("git_branch", sa.Text(), nullable=False, server_default="main"))
    op.add_column(
        "bots",
        sa.Column("git_token_enc", sa.Text(), nullable=False, server_default="", comment="enc"),
    )
    op.add_column("bots", sa.Column("git_last_backup_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE bots SET memory_snapshot_at = snapshots.last_collected FROM "
        "(SELECT bot_id, max(updated_at) AS last_collected FROM memories GROUP BY bot_id) "
        "snapshots WHERE bots.id = snapshots.bot_id"
    )


def downgrade():
    for name in (
        "git_last_backup_at",
        "git_token_enc",
        "git_branch",
        "git_url",
        "workspace_operation_id",
        "workspace_target_relay_id",
        "workspace_target_dir",
        "workspace_error",
        "workspace_state",
        "workspace_phase",
        "workspace_deadline",
        "memory_snapshot_at",
    ):
        op.drop_column("bots", name)
