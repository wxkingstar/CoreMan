"""M3a：交互待答状态 interaction_states 与公告 announcements

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# 与模型侧 interactions._in() / SCOPE_TARGET_CHECK 逐字一致。
CK_KIND = "kind IN ('choice', 'relay_switch', 'session_switch')"
CK_STATUS = "status IN ('open', 'submitted', 'cancelled', 'expired')"
CK_SCOPE = "scope IN ('global', 'relay', 'bot')"
CK_SCOPE_TARGET = (
    "(scope = 'global' AND relay_server_id IS NULL AND bot_id IS NULL) OR "
    "(scope = 'relay' AND relay_server_id IS NOT NULL AND bot_id IS NULL) OR "
    "(scope = 'bot' AND bot_id IS NOT NULL AND relay_server_id IS NULL)"
)


def _trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def _ts(name: str, *, null: bool = False, now: bool = False) -> sa.Column[sa.DateTime]:
    kw: dict[str, Any] = {"nullable": null}
    if now:
        kw["server_default"] = sa.text("now()")
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "interaction_states",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_interaction_states_bot_id_bots"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("task_id_prefix", sa.Text()),
        sa.Column(
            "state", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        _ts("expires_at", null=True),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        sa.CheckConstraint(CK_KIND, name="kind"),
        sa.CheckConstraint(CK_STATUS, name="status"),
        sa.UniqueConstraint(
            "kind",
            "scope_key",
            name="uq_interaction_states_kind",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index(
        "interaction_states_prefix_idx", "interaction_states", ["bot_id", "task_id_prefix"]
    )
    op.create_index(
        "interaction_states_expires_idx",
        "interaction_states",
        ["expires_at"],
        postgresql_where=sa.text("status = 'open' AND expires_at IS NOT NULL"),
    )
    _trigger("interaction_states")

    op.create_table(
        "announcements",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column(
            "relay_server_id",
            sa.Uuid(),
            sa.ForeignKey(
                "relay_servers.id",
                ondelete="CASCADE",
                name="fk_announcements_relay_server_id_relay_servers",
            ),
        ),
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_announcements_bot_id_bots"),
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        _ts("start_at", null=True),
        _ts("end_at", null=True),
        sa.Column("created_by", sa.Uuid()),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        sa.CheckConstraint(CK_SCOPE, name="scope"),
        sa.CheckConstraint(CK_SCOPE_TARGET, name="scope_target"),
    )
    op.create_index("announcements_active_idx", "announcements", ["scope", "is_active"])
    _trigger("announcements")


def downgrade() -> None:
    op.drop_table("announcements")
    op.drop_table("interaction_states")
