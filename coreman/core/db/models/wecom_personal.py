"""企业微信个人工具的授权记录：一个机器人、一位本人一行。加密字段永不下发给客户端。

企业微信的「可使用权限」绑在机器人上：谁在机器人编辑页授权，机器人就代谁操作。所以这里
记的不是用户令牌，而是「这位本人就是机器人的授权人」这件事，以及本人选的档位。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin, enum_check

WECOM_PERSONAL_STATUSES = ("selecting", "connected", "revoked")
WECOM_PERSONAL_LEVELS = ("readonly", "all_except_send", "all")


class WecomPersonalGrant(TimestampMixin, Base):
    __tablename__ = "wecom_personal_grants"
    __table_args__ = (
        CheckConstraint(enum_check("status", WECOM_PERSONAL_STATUSES), name="status"),
        CheckConstraint(
            enum_check("authorization_level", WECOM_PERSONAL_LEVELS), name="authorization_level"
        ),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    status: Mapped[str] = mapped_column(Text, default="revoked")
    authorization_level: Mapped[str] = mapped_column(Text, default="readonly")
    # 任何授权变动都换一个：签给运行时的能力凭据按它作废。
    context_epoch: Mapped[uuid.UUID] = mapped_column(
        Uuid, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    # 企业微信 whoami 返回的授权人 ID，连接时已与本人私聊的发送者核对一致。
    authorizer_id: Mapped[str | None] = mapped_column(Text)
    # 最近一次向企业微信核对授权人仍是本人的时间。
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 企业微信网关的访问令牌（机器人级，用 Bot ID 与 Secret 签名换取）；只在 connected 时保存。
    token_enc: Mapped[str | None] = mapped_column(Text)
    # Bot ID 与 Secret 的指纹：机器人换了凭证，旧令牌与授权人核对一并作废。
    bot_fingerprint: Mapped[str] = mapped_column(Text, default="", server_default="")
    # 档位卡片：发给哪个私聊、由哪一轮发出、什么时候过期。
    selection_chat_id: Mapped[str | None] = mapped_column(Text)
    selection_task_id: Mapped[int | None] = mapped_column(BigInteger)
    selection_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
