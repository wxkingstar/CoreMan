"""Runtime enrollment and reverse transport, independent of API process affinity."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column(n, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
        for n in ("created_at", "updated_at")
    ]


def upgrade() -> None:
    op.create_table(
        "runtime_nodes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        *[
            sa.Column(n, sa.Text(), nullable=False)
            for n in (
                "name",
                "token_hash",
                "workspace_root",
                "hostname",
                "username",
                "platform",
                "architecture",
                "environment",
                "version",
            )
        ],
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id")),
        sa.Column("visibility", sa.Text(), nullable=False, server_default="all"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("draining", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("capabilities", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("service_status", sa.Text(), nullable=False, server_default="unknown"),
        *timestamps(),
    )
    op.create_table(
        "runtime_install_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        *[
            sa.Column(n, sa.Text(), nullable=False)
            for n in ("name", "token_hash", "workspace_root")
        ],
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id")),
        sa.Column("visibility", sa.Text(), nullable=False, server_default="all"),
        sa.Column("options", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("node_id", sa.Uuid(), sa.ForeignKey("runtime_nodes.id")),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        *timestamps(),
    )
    op.add_column("relay_servers", sa.Column("runtime_node_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_relay_servers_runtime_node_id_runtime_nodes",
        "relay_servers",
        "runtime_nodes",
        ["runtime_node_id"],
        ["id"],
    )
    op.create_index("ix_relay_servers_runtime_node_id", "relay_servers", ["runtime_node_id"])
    op.create_table(
        "runtime_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "node_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("request_enc", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("status_code", sa.Integer()),
        sa.Column("content_type", sa.Text()),
        sa.Column("next_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumer_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_runtime_calls_node_id", "runtime_calls", ["node_id"])
    op.create_index("ix_runtime_calls_deadline", "runtime_calls", ["deadline"])
    op.create_table(
        "runtime_chunks",
        sa.Column(
            "call_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_calls.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("data_enc", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("runtime_chunks")
    op.drop_table("runtime_calls")
    op.drop_column("relay_servers", "runtime_node_id")
    op.drop_table("runtime_install_links")
    op.drop_table("runtime_nodes")
