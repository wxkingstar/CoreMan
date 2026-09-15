"""Self-enrolled runtime nodes and the cross-process reverse HTTP mailbox."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin


class RuntimeNode(TimestampMixin, Base):
    __tablename__ = "runtime_nodes"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("teams.id"))
    visibility: Mapped[str] = mapped_column(Text, server_default="all")
    workspace_root: Mapped[str] = mapped_column(Text)
    hostname: Mapped[str] = mapped_column(Text)
    username: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    architecture: Mapped[str] = mapped_column(Text)
    environment: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    draining: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    service_status: Mapped[str] = mapped_column(Text, server_default="unknown")
    # 节点上报的协议版本；旧节点不上报时为空。
    protocol_version: Mapped[int | None] = mapped_column(Integer)


class RuntimeInstallLink(TimestampMixin, Base):
    __tablename__ = "runtime_install_links"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(Text)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("teams.id"))
    visibility: Mapped[str] = mapped_column(Text, server_default="all")
    workspace_root: Mapped[str] = mapped_column(Text)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("runtime_nodes.id"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeCall(Base):
    __tablename__ = "runtime_calls"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runtime_nodes.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(Text)
    request_enc: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    status_code: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(Text)
    next_seq: Mapped[int] = mapped_column(Integer, server_default="0")
    response_bytes: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumer_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class RuntimeChunk(Base):
    __tablename__ = "runtime_chunks"
    call_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runtime_calls.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    data_enc: Mapped[str] = mapped_column(Text)
