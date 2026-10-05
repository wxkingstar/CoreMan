"""个人凭证：按（AI 员工, 用户）逐条加密保存的环境变量，以及一次向本人索取的请求。

值只以密文存在（AAD 绑定行身份，见 core/personal_credentials/policy.py）；请求里只有键名、
标签与用途，唯一的例外是一次性交付的密文，只留到续接轮结束。来源任务与入站事件不建外键：
它们按自己的保留期清理，不能被这里挡住。
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
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin, enum_check

CREDENTIAL_REQUEST_STATUSES = ("open", "submitted", "expired", "cancelled")
CREDENTIAL_ORIGINS = ("chat", "cron")


class PersonalCredential(TimestampMixin, Base):
    __tablename__ = "personal_credentials"
    __table_args__ = (
        UniqueConstraint("bot_id", "user_id", "env_key", name="uq_personal_credentials_bot_id"),
        Index("personal_credentials_user_idx", "user_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    env_key: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # false 的字段（例如账号）在「我的凭证」页显示原值；true 的永不显示。
    secret: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    value_enc: Mapped[str] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CredentialRequest(TimestampMixin, Base):
    __tablename__ = "credential_requests"
    __table_args__ = (
        CheckConstraint(enum_check("status", CREDENTIAL_REQUEST_STATUSES), name="status"),
        CheckConstraint(enum_check("origin_kind", CREDENTIAL_ORIGINS), name="origin_kind"),
        Index("credential_requests_owner_idx", "bot_id", "user_id"),
        Index(
            "credential_requests_open_idx",
            "expires_at",
            postgresql_where=text("status = 'open'"),
        ),
        Index(
            "credential_requests_handoff_idx",
            "submitted_at",
            postgresql_where=text("handoff_enc IS NOT NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    origin_kind: Mapped[str] = mapped_column(Text)
    origin_task_id: Mapped[int | None] = mapped_column(BigInteger)
    origin_event_id: Mapped[int | None] = mapped_column(BigInteger)
    origin_chat_id: Mapped[str] = mapped_column(Text)
    origin_chat_type: Mapped[str] = mapped_column(Text)
    origin_session_key: Mapped[str | None] = mapped_column(Text)
    # 提问那一轮用的 relay 会话：续接前核对它仍是该会话键当前的会话。
    origin_relay_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    cron_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # 表单或链接实际发到的会话；「已保存」等通知也发到这里。
    delivery_chat_id: Mapped[str | None] = mapped_column(Text)
    # [{key, label, secret, placeholder}]，不含任何值。
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    purpose: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # false：一次性交付，值不进 personal_credentials，只给提交后的续接轮用一次。
    save: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # 一次性交付的值（JSON 密文，AAD 绑定本请求）；续接轮结束、或续接不成立时擦掉。
    handoff_enc: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_outbox_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("outbox.id", ondelete="SET NULL")
    )
    resume_task_id: Mapped[int | None] = mapped_column(BigInteger)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
