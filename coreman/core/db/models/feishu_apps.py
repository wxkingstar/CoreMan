"""扫码创建 / 更新飞书应用的服务端会话。加密字段永不下发给客户端。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin, enum_check

REGISTRATION_PURPOSES = ("create", "update")
REGISTRATION_STATUSES = (
    "pending",
    "succeeded",
    "consumed",
    "expired",
    "denied",
    "failed",
    "cancelled",
)


class FeishuAppRegistration(TimestampMixin, Base):
    __tablename__ = "feishu_app_registrations"
    __table_args__ = (
        CheckConstraint(enum_check("purpose", REGISTRATION_PURPOSES), name="purpose"),
        CheckConstraint(enum_check("status", REGISTRATION_STATUSES), name="status"),
        Index("ix_feishu_app_registrations_user_id", "user_id", "created_at"),
        Index("ix_feishu_app_registrations_bot_id", "bot_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    # update：要补齐权限的机器人；create：消费它的机器人（消费前为空）。
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE")
    )
    purpose: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    # {"device_code", "url"}；扫码结束即清空。
    pending_enc: Mapped[str | None] = mapped_column(Text)
    # pending 时是二维码过期时间；succeeded 时是应用密钥的暂存期限。
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    poll_interval: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    app_id: Mapped[str | None] = mapped_column(Text)
    # 只在 succeeded 到被机器人消费之间保存；消费、过期或取消后清空。
    secret_enc: Mapped[str | None] = mapped_column(Text)
    owner_open_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # 从私聊卡片发起的补齐：{"chat_id", "message_id", "task_id"}。scheduler 替发起人取回结果
    # 并写回这张卡片；管理台发起的为空，由页面自己轮询。
    notify_card: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
