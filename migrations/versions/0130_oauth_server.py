"""CoreMan as an OAuth authorization server for members' local agents and the command line:
dynamically registered loopback clients, grants with rotating refresh tokens, access tokens."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.Text(), primary_key=True),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("redirect_uris", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("created_ip", postgresql.INET(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("oauth_clients_ip_idx", "oauth_clients", ["created_ip", "created_at"])
    op.create_table(
        "oauth_grants",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "client_id",
            sa.Text(),
            sa.ForeignKey("oauth_clients.client_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("refresh_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("previous_refresh_hash", sa.Text(), nullable=True, unique=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("oauth_grants_user_idx", "oauth_grants", ["user_id", "created_at"])
    op.create_table(
        "oauth_access_tokens",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column(
            "grant_id",
            sa.Uuid(),
            sa.ForeignKey("oauth_grants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_oauth_access_tokens_grant_id", "oauth_access_tokens", ["grant_id"])
    op.create_index("oauth_access_tokens_expiry_idx", "oauth_access_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_table("oauth_access_tokens")
    op.drop_table("oauth_grants")
    op.drop_table("oauth_clients")
