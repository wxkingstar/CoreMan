"""对话日志。不挂 bots 外键：bot 删除后日志保留。

对话一轮一行：开流时以 `running` 写入，收尾时原地改成终态。开流前就结束的轮次（relay 不可用、
失联任务）只在结束时写一行终态。
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
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from coreman.core.db.base import Base

CHAT_LOG_RUNNING = "running"
CHAT_LOG_STATUSES = (
    CHAT_LOG_RUNNING,
    "success",
    "error",
    "timeout",
    "stopped",
    "ask_user",
    "failed",
)
CHAT_TYPES = ("single", "group", "cron")


class ChatLog(Base):
    __tablename__ = "chat_logs"
    __table_args__ = (
        CheckConstraint("chat_type IN ('single','group','cron')", name="chat_type"),
        CheckConstraint(
            "status IN ('running','success','error','timeout','stopped','ask_user','failed')",
            name="status",
        ),
        Index("chat_logs_time_idx", "request_at"),
        Index("chat_logs_bot_time_idx", "bot_id", text("request_at DESC")),
        Index("chat_logs_user_time_idx", "user_id", text("request_at DESC")),
        Index("chat_logs_session_idx", "bot_id", "session_key", "relay_session_id", "request_at"),
        Index("chat_logs_status_idx", "status", text("request_at DESC")),
        # 结束时按任务找行：改进行中的那一行，或确认这一轮还没写过再补一行。
        Index("chat_logs_task_idx", "task_id"),
        # 一个任务最多一行进行中。旧版本在收尸与收尾交错时可能给同一任务留下两行终态，
        # 所以唯一只约束进行中的行，历史数据不影响升级。
        Index(
            "chat_logs_running_task_uk",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    bot_key: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    platform_user_id: Mapped[str | None] = mapped_column(Text)
    user_login: Mapped[str | None] = mapped_column(Text)
    user_name: Mapped[str | None] = mapped_column(Text)
    chat_type: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    session_key: Mapped[str | None] = mapped_column(Text)
    relay_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    relay_server_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model: Mapped[str | None] = mapped_column(Text)
    stream_id: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    message_type: Mapped[str] = mapped_column(Text)
    message_content: Mapped[str | None] = mapped_column(Text)
    quoted_content: Mapped[str | None] = mapped_column(Text)
    file_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response_content: Mapped[str | None] = mapped_column(Text)
    tools_used: Mapped[list[str]] = mapped_column(ARRAY(Text()), server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_creation_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    request_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 这一轮挂了本人的企业微信工具：记录里可能有本人的邮件、文档等资料，只给本人看。
    private: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
