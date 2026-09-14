"""Persist alert edges across scheduler failover."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT NOT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE c.relname='tasks_finished_idx' AND n.nspname=current_schema()"
                )
            )
            .scalar()
        )
        if invalid:
            op.execute("DROP INDEX CONCURRENTLY tasks_finished_idx")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS tasks_finished_idx "
            "ON tasks (finished_at) WHERE finished_at IS NOT NULL"
        )
    op.create_table(
        "alert_states",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("firing", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_key", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS tasks_finished_idx")
    op.drop_table("alert_states")
