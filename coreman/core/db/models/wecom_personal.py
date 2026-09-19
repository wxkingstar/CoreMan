"""企业微信个人工具：每位成员一份的授权绑定。加密字段永不下发给客户端。

企业微信的「可使用权限」绑在机器人上，机器人代表创建它的那位成员，所以按人授权的做法是：
每位成员自己扫码建一个只负责取数的「授权机器人」，CoreMan 托管它的凭证；成员在任意企业微信
AI 员工的私聊里，工具都用这份凭证、以本人身份执行。

`WecomPersonalGrant`（一个 AI 员工、一位成员一行）是上一版的做法，表留给滚动升级期间的旧版本，
新代码不再读写。
"""

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
    Index,
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin, enum_check

WECOM_PERSONAL_STATUSES = ("selecting", "connected", "revoked")
WECOM_PERSONAL_LEVELS = ("readonly", "all_except_send", "all")
WECOM_BINDING_STATUSES = ("unbound", "bound")
WECOM_SCAN_STATUSES = ("pending", "succeeded", "expired", "failed", "cancelled")


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


class WecomPersonalBinding(TimestampMixin, Base):
    __tablename__ = "wecom_personal_bindings"
    __table_args__ = (
        CheckConstraint(enum_check("status", WECOM_BINDING_STATUSES), name="status"),
        CheckConstraint(
            enum_check("authorization_level", WECOM_PERSONAL_LEVELS), name="authorization_level"
        ),
        CheckConstraint(
            "scan_status IS NULL OR " + enum_check("scan_status", WECOM_SCAN_STATUSES),
            name="scan_status",
        ),
        Index(
            "ix_wecom_personal_bindings_scan_due",
            "scan_next_poll_at",
            postgresql_where=text("scan_status = 'pending'"),
        ),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # bound：托管着一份已核对授权人就是本人的凭证。
    status: Mapped[str] = mapped_column(Text, default="unbound", server_default="unbound")
    # 本人在私聊里「断开」只是暂停使用，凭证还在；「连接」无需重新扫码即可恢复。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    authorization_level: Mapped[str] = mapped_column(
        Text, default="readonly", server_default="readonly"
    )
    # 任何授权变动都换一个：签给运行时的能力凭据按它作废。
    context_epoch: Mapped[uuid.UUID] = mapped_column(
        Uuid, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    wecom_bot_id: Mapped[str | None] = mapped_column(Text)
    # {"bot_id", "secret"}：授权机器人的凭证，只在服务端解密使用。
    credentials_enc: Mapped[str | None] = mapped_column(Text)
    bot_fingerprint: Mapped[str] = mapped_column(Text, default="", server_default="")
    # 企业微信网关的访问令牌（约 24 小时），过期用凭证重新签名换取。
    token_enc: Mapped[str | None] = mapped_column(Text)
    # whoami 返回的授权人 ID，绑定时已核对就是本人。
    authorizer_id: Mapped[str | None] = mapped_column(Text)
    authorizer_name: Mapped[str | None] = mapped_column(Text)
    # 授权机器人在企业微信里的名字：续期指引要告诉本人去哪个机器人的「可使用权限」。
    bot_name: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 凭证失效等需要本人处理的问题（credentials_rejected …）。
    error: Mapped[str | None] = mapped_column(Text)
    # 每项能力的最近状态：{"todo:read": {"state", "checked_at", "authorized_at", "renew_url"}}。
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # 最近一次发送到期提醒的时间。
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 扫码会话：{"scode", "auth_url"} 加密保存，拿到结果、过期或取消即清空。
    scan_status: Mapped[str | None] = mapped_column(Text)
    scan_enc: Mapped[str | None] = mapped_column(Text)
    scan_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scan_next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scan_poll_interval: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    scan_upstream_status: Mapped[str | None] = mapped_column(Text)
    scan_error: Mapped[str | None] = mapped_column(Text)
    # 从私聊里发起绑定时记下来：{"bot_id", "chat_id", "task_id", "at"}，绑定结果回到这个私聊。
    scan_notify: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
