"""Durable, user-and-bot-bound OAuth grants. Never expose encrypted fields to clients."""

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

from coreman.core.db.base import Base, TimestampMixin, enum_check

SEND_STATUSES = ("drafted", "sent")


class FeishuPersonalGrant(TimestampMixin, Base):
    __tablename__ = "feishu_personal_grants"
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    app_id: Mapped[str] = mapped_column(Text)
    app_fingerprint: Mapped[str] = mapped_column(Text, default="")
    platform_user_id: Mapped[str] = mapped_column(Text)
    open_id: Mapped[str] = mapped_column(Text)
    tenant_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    context_epoch: Mapped[uuid.UUID] = mapped_column(
        Uuid, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    # 已停用：个人工具改为叠加在普通助手上，不再切换模式。上一版本发布期间仍会读写，下个版本删除。
    assistant_mode: Mapped[str] = mapped_column(Text, default="personal", server_default="personal")
    token_enc: Mapped[str | None] = mapped_column(Text)
    pending_enc: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    authorization_level: Mapped[str] = mapped_column(
        Text, default="legacy_readonly", server_default="legacy_readonly"
    )
    requested_scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    remote_revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    selection_chat_id: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    poll_interval: Mapped[int] = mapped_column(Integer, default=5)


class FeishuPersonalSend(TimestampMixin, Base):
    """发邮件的去重记录：同一 uuid 的重试不会再发一封。飞书邮件接口没有幂等参数。"""

    __tablename__ = "feishu_personal_sends"
    __table_args__ = (
        CheckConstraint(enum_check("status", SEND_STATUSES), name="status"),
        Index("feishu_personal_sends_created_idx", "created_at"),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    send_key: Mapped[str] = mapped_column(Text, primary_key=True)
    # 请求内容的摘要：同一 uuid 换了收件人或正文是模型出错，不能当成重试。
    fingerprint: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    draft_id: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


RATE_CATEGORIES = ("chats", "history")


class FeishuRateUsage(Base):
    """以所有者身份调飞书的用量流水：一行一次调用，或一笔限频后占满额度的退避。

    飞书按「应用 × 企业 × 接口」限频，全员共用，所以按应用、按接口类别统计最近 60 秒的
    `sum(units)`。只插入不更新：计数器的行锁会被调用方的事务一直占着。旧行由盯消息循环清理。
    """

    __tablename__ = "feishu_rate_usage"
    __table_args__ = (
        CheckConstraint(enum_check("category", RATE_CATEGORIES), name="category"),
        CheckConstraint("units > 0", name="units"),
        Index("feishu_rate_usage_window_idx", "app_id", "category", "at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    app_id: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    units: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
