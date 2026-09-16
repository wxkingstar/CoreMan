"""基础设施授权

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column(n, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
        for n in ("created_at", "updated_at")
    ]


def upgrade() -> None:
    op.create_table(
        "systems",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("base_url", sa.Text()),
        sa.Column("sitemap_url", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "default_for_all_bots", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("allowed_bot_ids", pg.ARRAY(sa.Uuid())),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        *timestamps(),
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,49}$'", name="key_format"),
    )
    op.create_table(
        "bot_system_grants",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "system_key",
            sa.Text(),
            sa.ForeignKey("systems.key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("granted_by", sa.Uuid()),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "system_grant_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("bot_id", sa.Uuid()),
        sa.Column("requested", pg.ARRAY(sa.Text())),
        sa.Column("approved", pg.ARRAY(sa.Text())),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column("comment", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "api_clients",
        sa.Column("app_key", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("secret_enc", sa.Text(), nullable=False, comment="enc"),
        sa.Column("scopes", pg.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        *timestamps(),
    )
    op.create_table(
        "jwt_keys",
        sa.Column("kid", sa.Text(), primary_key=True),
        sa.Column("private_pem_enc", sa.Text(), nullable=False, comment="enc"),
        sa.Column("public_jwk", pg.JSONB(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "jwt_keys_one_active_idx",
        "jwt_keys",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    for table in ("systems", "api_clients"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in ("jwt_keys", "api_clients", "system_grant_audit", "bot_system_grants", "systems"):
        op.drop_table(table)
