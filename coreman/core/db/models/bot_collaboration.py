"""Opt-in Feishu routes and durable, single-hop collaboration ledger."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from coreman.core.db.base import Base


class BotCollaborationPartner(Base):
    """Administrator intent, independent of conversation transport identity."""

    __tablename__ = "bot_collaboration_partners"
    __table_args__ = (UniqueConstraint("source_bot_id", "target_bot_id"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE")
    )
    target_bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    timeout_seconds: Mapped[int] = mapped_column(Integer, server_default=text("300"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class BotCollaborationRoute(Base):
    __tablename__ = "bot_collaboration_routes"
    __table_args__ = (UniqueConstraint("source_bot_id", "target_bot_id", "chat_id"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE")
    )
    target_bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE")
    )
    chat_id: Mapped[str] = mapped_column(Text)
    tenant_key: Mapped[str] = mapped_column(Text)
    source_open_id: Mapped[str] = mapped_column(Text)
    target_open_id: Mapped[str] = mapped_column(Text)
    source_union_id: Mapped[str] = mapped_column(Text)
    target_union_id: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    timeout_seconds: Mapped[int] = mapped_column(Integer, server_default=text("300"))
    setup: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class BotCollaboration(Base):
    __tablename__ = "bot_collaborations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    route_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bot_collaboration_routes.id", ondelete="CASCADE")
    )
    source_task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), unique=True
    )
    origin_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    origin_platform_user_id: Mapped[str] = mapped_column(Text)
    source_session_key: Mapped[str] = mapped_column(Text)
    source_relay_session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source_relay_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'requested'"), index=True)
    request_outbox_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("outbox.id", ondelete="SET NULL")
    )
    response_outbox_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("outbox.id", ondelete="SET NULL")
    )
    helper_task_id: Mapped[int | None] = mapped_column(BigInteger)
    resume_task_id: Mapped[int | None] = mapped_column(BigInteger)
    response: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


HUMAN_ACTIVE = ("pending", "waiting", "answered", "resuming")
HUMAN_STATUSES = (*HUMAN_ACTIVE, "completed", "failed", "cancelled", "timed_out")
_HUMAN_ACTIVE_SQL = "status IN (" + ", ".join(repr(s) for s in HUMAN_ACTIVE) + ")"


class BotHumanPartner(Base):
    """A colleague the source employee may ask; configuring grants no bot permission."""

    __tablename__ = "bot_human_partners"
    __table_args__ = (UniqueConstraint("source_bot_id", "user_id"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    responsibility: Mapped[str] = mapped_column(Text, server_default=text("''"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __mapper_args__ = {"version_id_col": version}


class HumanCollaboration(Base):
    """One question from an employee task to one colleague, resumed by a real reply event."""

    __tablename__ = "human_collaborations"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(repr(s) for s in HUMAN_STATUSES) + ")", name="status"
        ),
        CheckConstraint("channel IN ('group','direct')", name="channel"),
        CheckConstraint("origin_kind IN ('chat','cron')", name="origin_kind"),
        Index(
            "human_collaborations_active_idx",
            "bot_id",
            "status",
            postgresql_where=text(_HUMAN_ACTIVE_SQL),
        ),
        Index(
            "human_collaborations_helper_idx",
            "helper_user_id",
            postgresql_where=text(_HUMAN_ACTIVE_SQL),
        ),
        Index("human_collaborations_message_idx", "bot_id", "request_message_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    partner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bot_human_partners.id", ondelete="CASCADE")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    helper_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    # Snapshot of the helper's Feishu user_id at ask time: replies must come from this account.
    helper_platform_user_id: Mapped[str] = mapped_column(Text)
    source_task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), unique=True
    )
    origin_kind: Mapped[str] = mapped_column(Text)
    origin_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    origin_platform_user_id: Mapped[str] = mapped_column(Text)
    origin_chat_id: Mapped[str | None] = mapped_column(Text)
    origin_chat_type: Mapped[str] = mapped_column(Text)
    origin_event_id: Mapped[int | None] = mapped_column(BigInteger)
    relay_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    cron_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    cron_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    channel: Mapped[str] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    request_outbox_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("outbox.id", ondelete="SET NULL")
    )
    request_message_id: Mapped[str | None] = mapped_column(Text)
    reminder_message_id: Mapped[str | None] = mapped_column(Text)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_event_id: Mapped[int | None] = mapped_column(BigInteger)
    response: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_task_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
