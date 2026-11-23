"""Claude 后台阶段：主回合结束后进程带着后台任务长驻，一个会话一行。

流程见 core/chat/background_runs.py。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from coreman.core.db.base import Base, enum_check

BACKGROUND_STATES = ("running", "finished", "stopped", "expired", "lost")


class BackgroundRun(Base):
    """一段后台阶段。同一 (bot_id, session_key) 最多一行 `running`。

    `owner_platform_user_id` 是进程里凭据所属的人（进入后台阶段那一轮的提问人），也是发给驱动的
    `live_owner`。`secrets_enc` 是那一轮注入进程的凭据值（加密的 JSON 数组）：后台续出的回合、
    本人接着发的消息都还在这个进程里跑，出站文本要按同一份过滤。
    """

    __tablename__ = "background_runs"
    __table_args__ = (
        CheckConstraint(enum_check("state", BACKGROUND_STATES), name="state"),
        Index(
            "background_runs_running_uq",
            "bot_id",
            "session_key",
            unique=True,
            postgresql_where=text("state = 'running'"),
        ),
        Index("background_runs_open_idx", "deadline", postgresql_where=text("state = 'running'")),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    session_key: Mapped[str] = mapped_column(Text)
    relay_session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # 不设外键：任务按保留期清理，后台阶段的记录不该挡住它。
    origin_task_id: Mapped[int] = mapped_column(BigInteger)
    owner_platform_user_id: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    relay_server_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    platform: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(Text)
    chat_type: Mapped[str] = mapped_column(Text)
    # 进入后台阶段那一轮回复的那条用户消息：完成回复都挂在它下面。
    origin_message_id: Mapped[str | None] = mapped_column(Text)
    # 后台任务快照（驱动的 Task 形状），按 id 合并。
    tasks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    # 请求过 close 的原因（user / expired / reset）；驱动收尾后据此定终态。
    close_reason: Mapped[str | None] = mapped_column(Text)
    close_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 要更新的那张卡片：飞书是 {"task_id": 进入后台阶段那一轮}，网关按它找卡片实体。
    card_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    secrets_enc: Mapped[str | None] = mapped_column(Text)
    # 后台续出、已经单独发出去的回合数（并入新消息那一轮的不算）。
    turns: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    card_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 节点心跳里第一次不见这个会话的时刻：持续不见才判丢失。
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
