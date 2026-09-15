"""交互待答状态与公告（`interaction_states`、`announcements`）。

`interaction_states` 是 AskUserQuestion / 限流切换 / 会话切换三种「等用户下一步」的状态；
`(kind, scope_key)` 唯一且 DEFERRABLE，数据访问层用「删旧插新」覆盖同 scope 的旧状态。
`announcements` 的 scope 与目标列互斥由 CHECK 保证，API 层不必再各自校验一遍。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from coreman.core.db.base import Base, TimestampMixin, enum_check

INTERACTION_KINDS = ("choice", "relay_switch", "session_switch")
INTERACTION_STATUSES = ("open", "submitted", "cancelled", "expired")
ANNOUNCEMENT_SCOPES = ("global", "relay", "bot")
# 与迁移 0005 逐字一致（alembic.ext.checkconstraint_byname 按名字比对，正文也保持相同）。
SCOPE_TARGET_CHECK = (
    "(scope = 'global' AND relay_server_id IS NULL AND bot_id IS NULL) OR "
    "(scope = 'relay' AND relay_server_id IS NOT NULL AND bot_id IS NULL) OR "
    "(scope = 'bot' AND bot_id IS NOT NULL AND relay_server_id IS NULL)"
)


class InteractionState(TimestampMixin, Base):
    """一条「等用户下一步」的状态；`state` 的形状按 kind 各不相同（见 chat/interactions.py）。"""

    __tablename__ = "interaction_states"
    __table_args__ = (
        CheckConstraint(enum_check("kind", INTERACTION_KINDS), name="kind"),
        CheckConstraint(enum_check("status", INTERACTION_STATUSES), name="status"),
        UniqueConstraint(
            "kind",
            "scope_key",
            name="uq_interaction_states_kind",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("interaction_states_prefix_idx", "bot_id", "task_id_prefix"),
        Index(
            "interaction_states_expires_idx",
            "expires_at",
            postgresql_where=text("status = 'open' AND expires_at IS NOT NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    scope_key: Mapped[str] = mapped_column(Text)
    task_id_prefix: Mapped[str | None] = mapped_column(Text)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Announcement(TimestampMixin, Base):
    """公告：命中即拦截对话。scope 决定 relay_server_id / bot_id 哪个非空。"""

    __tablename__ = "announcements"
    __table_args__ = (
        CheckConstraint(enum_check("scope", ANNOUNCEMENT_SCOPES), name="scope"),
        CheckConstraint(SCOPE_TARGET_CHECK, name="scope_target"),
        Index("announcements_active_idx", "scope", "is_active"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    scope: Mapped[str] = mapped_column(Text)
    relay_server_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("relay_servers.id", ondelete="CASCADE")
    )
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE")
    )
    content: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
