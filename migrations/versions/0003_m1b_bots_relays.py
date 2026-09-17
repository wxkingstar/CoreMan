"""机器人与运行时：relay_servers、model_catalog（含种子）、bots、bot_members、bot_allowed_users

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# 新安装系统的默认目录；修改此种子不会覆盖已安装系统的管理员配置。
# (provider, model, display_name, is_default, sort_order)
SEED_MODELS = [
    ("claude", "claude-sonnet-5", "Claude Sonnet 5", True, 100),
    ("claude", "claude-opus-5", "Claude Opus 5", False, 90),
    ("claude", "claude-haiku-4-5-20251001", "Claude Haiku 4.5", False, 80),
    ("claude", "claude-fable-5-1", "Claude Fable 5.1", False, 70),
    ("codex", "codex/gpt-6-astra", "GPT-6 Astra (Codex)", True, 100),
    ("codex", "codex/gpt-5.6-sol", "GPT-5.6 Sol (Codex)", False, 90),
    ("codex", "codex/gpt-5.6-terra", "GPT-5.6 Terra (Codex)", False, 80),
    ("codex", "codex/gpt-5.6-luna", "GPT-5.6 Luna (Codex)", False, 70),
]


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
    op.create_table(
        "relay_servers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("clawrelay_port", sa.Integer(), nullable=False),
        sa.Column("agent_port", sa.Integer()),
        sa.Column("ssh_user", sa.Text()),
        sa.Column("runtime_env", sa.Text(), server_default=sa.text("'host'"), nullable=False),
        sa.Column("chroot_path", sa.Text()),
        sa.Column("runtime_user", sa.Text()),
        sa.Column("model_provider", sa.Text(), nullable=False),
        sa.Column(
            "supported_models_mode",
            sa.Text(),
            server_default=sa.text("'inherit'"),
            nullable=False,
        ),
        sa.Column("supported_models", postgresql.ARRAY(sa.Text())),
        sa.Column(
            "team_id", sa.Uuid(), sa.ForeignKey("teams.id", name="fk_relay_servers_team_id_teams")
        ),
        sa.Column("visibility", sa.Text(), server_default=sa.text("'all'"), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("agent_token_enc", sa.Text(), comment="enc"),
        sa.Column("rate_limit_5h_used_pct", sa.Numeric(5, 2)),
        sa.Column("rate_limit_5h_resets_at", sa.DateTime(timezone=True)),
        sa.Column("rate_limit_7d_used_pct", sa.Numeric(5, 2)),
        sa.Column("rate_limit_7d_resets_at", sa.DateTime(timezone=True)),
        sa.Column("rate_limit_probed_at", sa.DateTime(timezone=True)),
        sa.Column("health_status", sa.Text(), server_default=sa.text("'unknown'"), nullable=False),
        sa.Column("health_checked_at", sa.DateTime(timezone=True)),
        sa.Column("health_detail", sa.Text()),
        sa.Column("health_latency_ms", sa.Integer()),
        sa.Column("health_fail_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("relay_version", sa.Text()),
        sa.Column("relay_mode", sa.Text()),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("name", name="uq_relay_servers_name"),
        sa.UniqueConstraint("host", "clawrelay_port", name="uq_relay_servers_host"),
        # CHECK 用短名做命名约定的种子，最终名由 ck_%(table_name)s_%(constraint_name)s 派生，
        # 与模型侧 CheckConstraint(name=...) 一致（见 0001 的长注释）。
        sa.CheckConstraint("runtime_env IN ('host','chroot','nspawn')", name="runtime_env"),
        sa.CheckConstraint(
            "supported_models_mode IN ('inherit','restricted')", name="supported_models_mode"
        ),
        sa.CheckConstraint("visibility IN ('all','admins')", name="visibility"),
        sa.CheckConstraint(
            "health_status IN ('healthy','down','auth_fail','timeout','unknown')",
            name="health_status",
        ),
    )
    _trigger("relay_servers")

    op.create_table(
        "model_catalog",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("retired", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("supports_xhigh", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("provider", "model", name="pk_model_catalog"),
    )
    _trigger("model_catalog")
    catalog = sa.table(
        "model_catalog",
        sa.column("provider", sa.Text),
        sa.column("model", sa.Text),
        sa.column("display_name", sa.Text),
        sa.column("is_default", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    # 幂等：目录行也可能由 relay 探测预填，重复的 (provider, model) 直接跳过。
    op.execute(
        postgresql.insert(catalog)
        .values(
            [
                {
                    "provider": p,
                    "model": m,
                    "display_name": d,
                    "is_default": df,
                    "sort_order": s,
                }
                for p, m, d, df, s in SEED_MODELS
            ]
        )
        .on_conflict_do_nothing()
    )

    op.create_table(
        "bots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("bot_key", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("avatar_url", sa.Text()),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id", name="fk_bots_team_id_teams")),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", name="fk_bots_created_by_users"),
            nullable=False,
        ),
        sa.Column(
            "relay_server_id",
            sa.Uuid(),
            sa.ForeignKey("relay_servers.id", name="fk_bots_relay_server_id_relay_servers"),
        ),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("working_dir", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("merged_system_prompt", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "verbosity_level", sa.SmallInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("effort_level", sa.Text()),
        sa.Column(
            "sse_timeout_seconds", sa.Integer(), server_default=sa.text("3600"), nullable=False
        ),
        sa.Column("agent_timeout_seconds", sa.Integer()),
        sa.Column("credentials_enc", sa.Text(), nullable=False, comment="enc"),
        sa.Column(
            "env_vars_enc",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
            comment="enc",
        ),
        sa.Column("welcome_message", sa.Text()),
        sa.Column("notify_webhook_url", sa.Text()),
        sa.Column(
            "custom_command_modules",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("bot_key", name="uq_bots_bot_key"),
        sa.CheckConstraint("bot_key ~ '^[a-z0-9][a-z0-9_-]{1,49}$'", name="bot_key"),
        sa.CheckConstraint("platform IN ('wecom','feishu')", name="platform"),
        sa.CheckConstraint("verbosity_level BETWEEN 1 AND 4", name="verbosity_level"),
        sa.CheckConstraint("effort_level IN ('low','medium','high','xhigh')", name="effort_level"),
        sa.CheckConstraint(
            "sse_timeout_seconds BETWEEN 1800 AND 43200", name="sse_timeout_seconds"
        ),
    )
    op.create_index("bots_relay_idx", "bots", ["relay_server_id"])
    _trigger("bots")

    op.create_table(
        "bot_members",
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_bot_members_bot_id_bots"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_bot_members_user_id_users"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), server_default=sa.text("'admin'"), nullable=False),
        sa.Column("added_by", sa.Uuid()),
        _ts("added_at"),
        sa.PrimaryKeyConstraint("bot_id", "user_id", name="pk_bot_members"),
        sa.CheckConstraint("role IN ('admin')", name="role"),
    )
    op.create_table(
        "bot_allowed_users",
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_bot_allowed_users_bot_id_bots"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey(
                "users.id", ondelete="CASCADE", name="fk_bot_allowed_users_user_id_users"
            ),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("bot_id", "user_id", name="pk_bot_allowed_users"),
    )


def downgrade() -> None:
    op.drop_table("bot_allowed_users")
    op.drop_table("bot_members")
    op.drop_index("bots_relay_idx", table_name="bots")
    op.drop_table("bots")
    op.drop_table("model_catalog")
    op.drop_table("relay_servers")
