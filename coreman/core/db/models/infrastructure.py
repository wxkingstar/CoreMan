"""M3b：系统授权、调用方凭证与 ES256 密钥。"""

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
    Identity,
    Index,
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin


class BusinessSystem(TimestampMixin, Base):
    __tablename__ = "systems"
    __table_args__ = (CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,49}$'", name="key_format"),)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)
    sitemap_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    default_for_all_bots: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    allowed_bot_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class BotSystemGrant(Base):
    __tablename__ = "bot_system_grants"
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    system_key: Mapped[str] = mapped_column(
        Text, ForeignKey("systems.key", ondelete="CASCADE"), primary_key=True
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class SystemGrantAudit(Base):
    """已弃用：原先只写不读，现已停止写入，授权变更以 audit_logs 的 bot.system_grants /
    system.update 为准。映射只为让模型与迁移保持一致（alembic check），不从 models 包导出、
    不得再读写；按 expand-only 规则表本身保留，下个版本随迁移一并删除。"""

    __tablename__ = "system_grant_audit"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    requested: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    approved: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ApiClient(TimestampMixin, Base):
    __tablename__ = "api_clients"
    app_key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    secret_enc: Mapped[str] = mapped_column(Text, comment="enc")
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class JwtKey(Base):
    __tablename__ = "jwt_keys"
    __table_args__ = (
        Index(
            "jwt_keys_one_active_idx", "is_active", unique=True, postgresql_where=text("is_active")
        ),
    )
    kid: Mapped[str] = mapped_column(Text, primary_key=True)
    private_pem_enc: Mapped[str] = mapped_column(Text, comment="enc")
    public_jwk: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
