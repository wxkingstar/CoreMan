"""授权融入使用过程的等待记录，以及群聊以提问人身份写入的审计。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, enum_check

AUTH_WAIT_STATUSES = ("offered", "waiting", "resumed", "expired")
AUTH_WAIT_LIVE = ("offered", "waiting")


class FeishuAuthWait(Base):
    """一轮因缺授权而停下的对话。offered 等用户点升级，waiting 等授权结果。"""

    __tablename__ = "feishu_auth_waits"
    __table_args__ = (
        CheckConstraint(enum_check("status", AUTH_WAIT_STATUSES), name="status"),
        Index(
            "feishu_auth_waits_live_uq",
            "bot_id",
            "user_id",
            "chat_id",
            unique=True,
            postgresql_where=text("status IN ('offered', 'waiting')"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(Text)
    origin_task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="SET NULL")
    )
    need: Mapped[str] = mapped_column(Text, default="general")
    status: Mapped[str] = mapped_column(Text, default="waiting")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class FeishuGroupAction(Base):
    """群里一次以提问人身份的写入。不存正文；同任务同参数成功后不再执行。"""

    __tablename__ = "feishu_group_actions"
    __table_args__ = (
        Index("feishu_group_actions_task_idx", "task_id", "tool", "args_hash"),
        Index("feishu_group_actions_user_idx", "bot_id", "user_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="SET NULL")
    )
    tool: Mapped[str] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    # succeeded / failed / pending_confirmation / cancelled
    status: Mapped[str] = mapped_column(Text)
    upstream_code: Mapped[int | None] = mapped_column(BigInteger)
    args_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
