"""云服务代理：调用流水、待审批挂起单、机器人审批人。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin, enum_check

CLOUD_RISKS = ("read", "write_low", "write_high")
CLOUD_OUTCOMES = ("ok", "http_error", "denied", "approval_required", "unreachable")
CLOUD_APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "executed", "withdrawn")


class CloudCall(Base):
    """代理的一次云服务调用（含被拒绝的）：只记元数据，不记参数和响应。

    不加外键，机器人、用户删除后仍可追溯。
    """

    __tablename__ = "cloud_calls"
    __table_args__ = (
        CheckConstraint(enum_check("risk", CLOUD_RISKS), name="risk"),
        CheckConstraint(enum_check("outcome", CLOUD_OUTCOMES), name="outcome"),
        Index("cloud_calls_task_idx", "task_id"),
        Index("cloud_calls_time_idx", "called_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    product: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    approval_id: Mapped[str | None] = mapped_column(Text)
    request_hash: Mapped[str | None] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class CloudPendingApproval(TimestampMixin, Base):
    """等待 Tower 审批的云服务写操作；external_id 是向 Tower 提交时的幂等键。

    不加外键：审批结果回来时机器人、任务可能已被清理，挂起单仍需能收尾。
    """

    __tablename__ = "cloud_pending_approvals"
    __table_args__ = (
        CheckConstraint(enum_check("status", CLOUD_APPROVAL_STATUSES), name="status"),
    )
    external_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    session_key: Mapped[str] = mapped_column(Text)
    origin_event_id: Mapped[int | None] = mapped_column(BigInteger)
    origin_relay_session_id: Mapped[str | None] = mapped_column(Text)
    product: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    approval_id: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 审批人填写的拒绝理由（已截断、废掉尖括号），续接提示里引用
    reason: Mapped[str | None] = mapped_column(Text)
    resume_task_id: Mapped[int | None] = mapped_column(BigInteger)


class BotApprover(Base):
    """bot 的云服务写操作审批人。"""

    __tablename__ = "bot_approvers"
    __table_args__ = (PrimaryKeyConstraint("bot_id", "user_id"),)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
