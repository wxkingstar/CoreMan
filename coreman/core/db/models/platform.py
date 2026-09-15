"""平台应用与通讯录同步记录（platform_apps、contact_sync_runs）。"""

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
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin
from coreman.core.db.models.account import PLATFORM_CHECK

CAPABILITIES = ("contact_sync", "login", "notify", "callback")


class PlatformApp(TimestampMixin, Base):
    __tablename__ = "platform_apps"
    __table_args__ = (CheckConstraint(PLATFORM_CHECK, name="platform"),)
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    platform: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(Text()))
    corp_id: Mapped[str | None] = mapped_column(Text)
    app_id: Mapped[str | None] = mapped_column(Text)
    secret_enc: Mapped[str] = mapped_column(Text, comment="enc")
    callback_token_enc: Mapped[str | None] = mapped_column(Text, comment="enc")
    callback_aes_key_enc: Mapped[str | None] = mapped_column(Text, comment="enc")
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    # 乐观锁：SQLAlchemy 自动 +1 并在 UPDATE/DELETE 上加 `WHERE version = :旧值`，
    # 匹配不到行就抛 StaleDataError（→409）。只比较 If-Match 不是原子的：两个带同一
    # If-Match 的并发 PUT 会双双成功，后写的静默覆盖先写的。
    __mapper_args__ = {"version_id_col": version}


class ContactSyncRun(Base):
    __tablename__ = "contact_sync_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running','success','failed','aborted')", name="status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    platform_app_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("platform_apps.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)
