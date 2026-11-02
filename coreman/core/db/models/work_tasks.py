"""任务跟进：调度员分派的任务，以及分两层（给人看 / 给 Agent 看）的时间线。

见 docs/superpowers/specs/2026-11-02-group-dispatcher-and-work-tasks.md。队列表 `tasks` 是
worker 的工作单，这里是对人可见的「一件事」，编号 `T-<序号>` 全部署共用一个序列。
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
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base

# shadow：调度试运行记下的「会这样分派」，不交接、不发群消息。
WORK_TASK_STATUSES = (
    "shadow",
    "pending",
    "queued",
    "running",
    "awaiting_confirmation",
    "done",
    "unfinished",
    "cancelled",
    "needs_human",
)
WORK_TASK_ACTORS = ("human", "bot", "system")


class WorkTask(Base):
    __tablename__ = "work_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('shadow','pending','queued','running','awaiting_confirmation',"
            "'done','unfinished','cancelled','needs_human')",
            name="status",
        ),
        UniqueConstraint("code", name="uq_work_tasks_code"),
        Index("work_tasks_chat_idx", "chat_id", "created_at"),
        Index("work_tasks_created_idx", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(
        Text, server_default=text("('T-'::text || nextval('work_task_code_seq'::regclass))")
    )
    title: Mapped[str] = mapped_column(Text)
    # 给人看的一句话：分派时写要做什么，完成时改成结果。
    summary: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(Text)
    origin_event_id: Mapped[int | None] = mapped_column(BigInteger)
    requester_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    requester_name: Mapped[str] = mapped_column(Text, server_default=text("''"))
    assignee_bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="SET NULL")
    )
    dispatcher_bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="SET NULL")
    )
    decision_id: Mapped[int | None] = mapped_column(BigInteger)
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("work_tasks.id", ondelete="SET NULL")
    )
    related_codes: Mapped[list[str]] = mapped_column(ARRAY(Text()), server_default=text("'{}'"))
    # 给执行员工的要求（Agent 层）：背景、验收标准、注意事项。
    brief: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # 完成时的任务记要（Agent 层），后续相关任务的背景。
    digest: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkTaskEvent(Base):
    """时间线上的一条：`human_text` 给人看（一行），`agent_detail` 给 Agent 看（展开）。"""

    __tablename__ = "work_task_events"
    __table_args__ = (
        CheckConstraint("actor_type IN ('human','bot','system')", name="actor_type"),
        Index("work_task_events_task_idx", "work_task_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    work_task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("work_tasks.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(Text)
    human_text: Mapped[str] = mapped_column(Text, server_default=text("''"))
    agent_detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    chat_task_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
