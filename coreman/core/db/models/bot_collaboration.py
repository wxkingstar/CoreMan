"""Opt-in Feishu routes and durable, single-hop collaboration ledger."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
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
