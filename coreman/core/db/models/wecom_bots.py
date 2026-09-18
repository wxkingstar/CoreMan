"""扫码创建企业微信智能机器人的服务端会话。加密字段永不下发给客户端。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin, enum_check

WECOM_PROVISION_STATUSES = ("pending", "succeeded", "consumed", "expired", "failed", "cancelled")


class WecomBotProvision(TimestampMixin, Base):
    __tablename__ = "wecom_bot_provisions"
    __table_args__ = (
        CheckConstraint(enum_check("status", WECOM_PROVISION_STATUSES), name="status"),
        Index("ix_wecom_bot_provisions_user_id", "user_id", "created_at"),
        Index("ix_wecom_bot_provisions_bot_id", "bot_id"),
        Index(
            "ix_wecom_bot_provisions_due",
            "next_poll_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    # 消费它的 CoreMan 机器人（消费前为空）。
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, default="pending")
    # {"scode", "auth_url"}：scode 本身就能取回密钥，拿到结果或会话结束即清空。
    pending_enc: Mapped[str | None] = mapped_column(Text)
    # pending 时是二维码过期时间；succeeded 时是密钥的暂存期限。
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 当前轮询间隔：网络错误时翻倍退避，恢复后回到默认值。
    poll_interval: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 企业微信返回的最近一次状态（init / pending …），只用于排障与前端提示。
    upstream_status: Mapped[str | None] = mapped_column(Text)
    wecom_bot_id: Mapped[str | None] = mapped_column(Text)
    # 只在 succeeded 到被机器人消费之间保存；消费、过期或取消后清空。
    secret_enc: Mapped[str | None] = mapped_column(Text)
    # 最近一次长连接订阅校验通过的时间；为空表示还没校验或校验没通过。
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
