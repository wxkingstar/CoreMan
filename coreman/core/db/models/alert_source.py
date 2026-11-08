"""告警来源：把群里某个告警机器人的消息绑定到一个固定的服务用户（见 docs/feishu.md）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base


class BotAlertSource(Base):
    """登记的告警来源：机器人 + 群 + 发送者 union_id，对应一个固定的服务用户。"""

    __tablename__ = "bot_alert_sources"
    __table_args__ = (
        UniqueConstraint("bot_id", "chat_id", "sender_union_id", name="bot_alert_sources_key"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(Text)
    sender_union_id: Mapped[str] = mapped_column(Text)
    service_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    dedupe_seconds: Mapped[int] = mapped_column(Integer, server_default=text("600"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class BotAlertState(Base):
    """一个告警来源下同名告警的去重状态。"""

    __tablename__ = "bot_alert_states"
    __table_args__ = (PrimaryKeyConstraint("source_id", "alert_key"),)
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bot_alert_sources.id", ondelete="CASCADE")
    )
    alert_key: Mapped[str] = mapped_column(Text)
    last_task_id: Mapped[int | None] = mapped_column(BigInteger)
    last_event_id: Mapped[int | None] = mapped_column(BigInteger)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    repeats: Mapped[int] = mapped_column(Integer, server_default=text("0"))
