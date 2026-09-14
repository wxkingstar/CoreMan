"""告警状态持久化，重启/主从切换不重复通知。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base


class AlertState(Base):
    __tablename__ = "alert_states"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    firing: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    generation: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    message_key: Mapped[str] = mapped_column(Text)
