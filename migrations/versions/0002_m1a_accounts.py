"""M1a：users.source、team_rules、user_identities、departments、user_departments、
auth_nonces、login_attempts、platform_apps、contact_sync_runs

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def _ts(name: str) -> sa.Column[sa.DateTime]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("source", sa.Text(), server_default=sa.text("'sync'"), nullable=False),
    )
    op.create_check_constraint("source", "users", "source IN ('sync','bootstrap','manual')")
    # M0 的引导登录把引导账号写成了普通 users 行；改成 source='bootstrap'、login_name=NULL，
    # 只保留最近登录的一条，其余（不同用户名反复引导产生的）停用。
    op.execute(
        """
        WITH b AS (
          SELECT DISTINCT user_id FROM admin_sessions WHERE auth_method = 'bootstrap'
        ), keep AS (
          SELECT u.id FROM users u JOIN b ON b.user_id = u.id
          ORDER BY u.last_login_at DESC NULLS LAST LIMIT 1
        )
        UPDATE users SET source = 'bootstrap', login_name = NULL,
               display_name = '引导管理员 (' || COALESCE(login_name, '') || ')'
        WHERE id IN (SELECT id FROM keep);
        """
    )
    op.execute(
        """
        UPDATE users SET status = 'disabled', login_name = NULL, source = 'manual'
        WHERE id IN (SELECT DISTINCT user_id FROM admin_sessions WHERE auth_method = 'bootstrap')
          AND source <> 'bootstrap';
        """
    )
    op.create_index(
        "users_bootstrap_uk",
        "users",
        ["source"],
        unique=True,
        postgresql_where=sa.text("source = 'bootstrap'"),
    )

    op.create_table(
        "team_rules",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "team_id",
            sa.Uuid(),
            sa.ForeignKey("teams.id", ondelete="CASCADE", name="fk_team_rules_team_id_teams"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text()),
        sa.Column("dept_path_contains", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
    )
    _trigger("team_rules")

    op.create_table(
        "user_identities",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_user_identities_user_id_users"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("platform_user_id", sa.Text(), nullable=False),
        sa.Column("open_id", sa.Text()),
        sa.Column("union_id", sa.Text()),
        sa.Column(
            "profile", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("platform IN ('wecom','feishu')", name="platform"),
        sa.UniqueConstraint("platform", "platform_user_id", name="uq_user_identities_platform"),
    )
    op.create_index("user_identities_open_id_idx", "user_identities", ["platform", "open_id"])
    _trigger("user_identities")

    op.create_table(
        "departments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("platform_dept_id", sa.Text(), nullable=False),
        sa.Column(
            "parent_id",
            sa.Uuid(),
            sa.ForeignKey("departments.id", name="fk_departments_parent_id_departments"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("platform", "platform_dept_id", name="uq_departments_platform"),
    )
    _trigger("departments")

    op.create_table(
        "user_departments",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_user_departments_user_id_users"),
            nullable=False,
        ),
        sa.Column(
            "department_id",
            sa.Uuid(),
            sa.ForeignKey(
                "departments.id",
                ondelete="CASCADE",
                name="fk_user_departments_department_id_departments",
            ),
            nullable=False,
        ),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "department_id", name="pk_user_departments"),
    )

    op.create_table(
        "auth_nonces",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("kind", "value", name="pk_auth_nonces"),
    )

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("ip", postgresql.INET(), nullable=False),
        _ts("attempted_at"),
    )
    op.create_index("login_attempts_ip_time_idx", "login_attempts", ["ip", "attempted_at"])

    op.create_table(
        "platform_apps",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("capabilities", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("corp_id", sa.Text()),
        sa.Column("app_id", sa.Text()),
        sa.Column("secret_enc", sa.Text(), nullable=False, comment="enc"),
        sa.Column("callback_token_enc", sa.Text(), comment="enc"),
        sa.Column("callback_aes_key_enc", sa.Text(), comment="enc"),
        sa.Column(
            "extra", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("platform IN ('wecom','feishu')", name="platform"),
    )
    _trigger("platform_apps")

    op.create_table(
        "contact_sync_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "platform_app_id",
            sa.Uuid(),
            sa.ForeignKey(
                "platform_apps.id",
                ondelete="CASCADE",
                name="fk_contact_sync_runs_platform_app_id_platform_apps",
            ),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("triggered_by", sa.Uuid()),
        _ts("started_at"),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "stats", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("status IN ('running','success','failed','aborted')", name="status"),
    )


def downgrade() -> None:
    op.drop_table("contact_sync_runs")
    op.drop_table("platform_apps")
    op.drop_index("login_attempts_ip_time_idx", table_name="login_attempts")
    op.drop_table("login_attempts")
    op.drop_table("auth_nonces")
    op.drop_table("user_departments")
    op.drop_table("departments")
    op.drop_index("user_identities_open_id_idx", table_name="user_identities")
    op.drop_table("user_identities")
    op.drop_table("team_rules")
    op.drop_index("users_bootstrap_uk", table_name="users")
    op.drop_constraint(op.f("ck_users_source"), "users", type_="check")
    op.drop_column("users", "source")
