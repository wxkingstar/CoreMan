"""群里读飞书文档时，提问人对单篇文档的放行。

见 docs/superpowers/specs/2026-11-06-feishu-group-docs.md。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, enum_check

DOC_CONSENT_STATUSES = ("pending", "approved", "declined", "expired")


class FeishuDocConsent(Base):
    """一张放行卡片：本群里本人对卡片上列出的几篇文档的同意。

    只对同一机器人、同一群、同一个人提问的回合生效；`tokens` 是链接里的文档 token
    （知识库是节点 token），工具只按链接取 token，对得上才以本人身份读。
    """

    __tablename__ = "feishu_doc_consents"
    __table_args__ = (
        CheckConstraint(enum_check("status", DOC_CONSENT_STATUSES), name="status"),
        Index("feishu_doc_consents_lookup_idx", "bot_id", "chat_id", "user_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(Text)
    tokens: Mapped[list[str]] = mapped_column(ARRAY(Text))
    urls: Mapped[list[str]] = mapped_column(ARRAY(Text))
    status: Mapped[str] = mapped_column(Text, default="pending")
    # 卡片上有没有「共享给本群」按钮；本人点了它，就是 shared。
    share_offered: Mapped[bool] = mapped_column(Boolean, default=False)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    origin_task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="SET NULL")
    )
    # 出站队列会被清理，不设外键：查不到就当卡片已经不在了。
    card_outbox_id: Mapped[int | None] = mapped_column(BigInteger)
    # pending 时是卡片的有效期，approved 时是放行的有效期。
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
