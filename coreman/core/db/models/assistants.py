"""个人助理的数据密钥。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base


class AssistantKey(Base):
    """每人一把数据密钥，用平台主密钥包裹；销毁后 dek_enc 为空，对应内容永久不可读。

    重新签发时 generation 加 1：内容密文记着自己是第几代加密的，旧一代的读不出来。
    """

    __tablename__ = "assistant_keys"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), primary_key=True)
    dek_enc: Mapped[str | None] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
