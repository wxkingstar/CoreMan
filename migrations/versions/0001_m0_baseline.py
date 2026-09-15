"""基线：teams、users、admin_sessions、audit_logs、settings

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPDATED_AT_FN = """
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = clock_timestamp(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
"""


def _trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def upgrade() -> None:
    op.execute(UPDATED_AT_FN)
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name_zh", sa.Text(), nullable=False),
        sa.Column("name_ja", sa.Text()),
        sa.Column("name_en", sa.Text()),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("slug", name="uq_teams_slug"),
    )
    _trigger("teams")
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("login_name", sa.Text()),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text()),
        sa.Column("mobile", sa.Text()),
        sa.Column("avatar_url", sa.Text()),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("locale", sa.Text(), server_default=sa.text("'zh'"), nullable=False),
        sa.Column("role", sa.Text(), server_default=sa.text("'member'"), nullable=False),
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id", name="fk_users_team_id_teams")),
        sa.Column("position", sa.Text()),
        sa.Column("skills", sa.Text()),
        sa.Column("bot_accessible", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "manual_fields",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("login_name", name="uq_users_login_name"),
        # NOTE: names below are the naming-convention *seed* ("constraint_name"
        # token), not the final constraint name. Base.metadata's naming
        # convention (ck_%(table_name)s_%(constraint_name)s) is also applied
        # here because migrations/env.py passes target_metadata to
        # context.configure(), and SQLAlchemy always re-derives a
        # CheckConstraint's name through any convention whose template
        # contains "%(constraint_name)s" -- even when a literal name is
        # already given (sqlalchemy/sql/naming.py:_constraint_name_for_table).
        # Passing the already-fully-qualified name here (e.g. "ck_users_status")
        # would therefore be doubled into "ck_users_ck_users_status". Seeding
        # with the short name matches coreman/core/db/models.py's
        # CheckConstraint(name="status") and both resolve to "ck_users_status".
        sa.CheckConstraint("status IN ('active','disabled')", name="status"),
        sa.CheckConstraint("locale IN ('zh','ja','en')", name="locale"),
        sa.CheckConstraint(
            "role IN ('platform_admin','ai_committee','team_lead','member')", name="role"
        ),
    )
    op.create_index(
        "users_email_uk",
        "users",
        [sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_index(
        "users_mobile_uk",
        "users",
        ["mobile"],
        unique=True,
        postgresql_where=sa.text("mobile IS NOT NULL"),
    )
    _trigger("users")
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_admin_sessions_user_id_users"),
            nullable=False,
        ),
        sa.Column("auth_method", sa.Text(), nullable=False),
        sa.Column("ip", postgresql.INET()),
        sa.Column("user_agent", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column("actor_login", sa.Text()),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text()),
        sa.Column("target_id", sa.Text()),
        sa.Column("diff", postgresql.JSONB()),
        sa.Column("ip", postgresql.INET()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "audit_logs_target_idx",
        "audit_logs",
        ["target_type", "target_id", sa.text("created_at DESC")],
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.Uuid()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_index("audit_logs_target_idx", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("admin_sessions")
    op.drop_index("users_mobile_uk", table_name="users")
    op.drop_index("users_email_uk", table_name="users")
    op.drop_table("users")
    op.drop_table("teams")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
