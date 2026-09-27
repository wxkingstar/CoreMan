"""飞书卡片投递状态与 worker 的生成状态分离，接管时可恢复卡片和序号。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base


class FeishuDelivery(Base):
    __tablename__ = "feishu_deliveries"
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("task_streams.task_id", ondelete="CASCADE"), primary_key=True
    )
    card_id: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)
    reaction_id: Mapped[str | None] = mapped_column(Text)
    reaction_done: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reaction_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reaction_failures: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    sequence: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    is_static: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    failures: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    fallback: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # 流式卡片上当前的单元布局与状态（思考预览、上次开启流式的时间），见 feishu_cards/stream.py。
    layout: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
