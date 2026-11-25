"""任务平台：人和 AI 员工之间的「一件事」，以及分两条线（给人看 / 给 Agent 看）的时间线。

见 docs/superpowers/specs/2026-11-25-company-task-platform.md。队列表 `tasks` 是 worker 的
工作单，这里是对人可见的「一件事」，编号 `T-<序号>` 全部署共用一个序列。执行人可以是人
（`assignee_user_id`），也可以是 AI 员工（`assignee_bot_id`），同一时刻只有一个。
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
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base

# shadow：调度试运行记下的「会这样分派」，不交接、不发群消息，默认列表里不显示。
WORK_TASK_STATUSES = (
    "shadow",
    "todo",
    "in_progress",
    "waiting",
    "review",
    "done",
    "cancelled",
)
WORK_TASK_OPEN = ("todo", "in_progress", "waiting", "review")
WORK_TASK_ACTORS = ("human", "bot", "system")
WORK_TASK_SOURCES = ("dispatch", "web", "agent", "api", "collaboration")
WORK_TASK_PRIORITIES = ("normal", "high", "urgent")
# waiting：这一轮派出子任务或问了人，任务在等结果。
WORK_TASK_RUN_STATUSES = ("running", "done", "confirm", "failed", "cancelled", "waiting")


class WorkTask(Base):
    __tablename__ = "work_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('shadow','todo','in_progress','waiting','review','done','cancelled')",
            name="status",
        ),
        CheckConstraint(
            "source IN ('dispatch','web','agent','api','collaboration')", name="source"
        ),
        CheckConstraint("priority IN ('normal','high','urgent')", name="priority"),
        CheckConstraint("start_mode IN ('auto','manual')", name="start_mode"),
        CheckConstraint("assignee_bot_id IS NULL OR assignee_user_id IS NULL", name="one_assignee"),
        UniqueConstraint("code", name="uq_work_tasks_code"),
        Index("work_tasks_chat_idx", "chat_id", "created_at"),
        Index("work_tasks_created_idx", "created_at"),
        Index("work_tasks_assignee_user_idx", "assignee_user_id", "status"),
        Index("work_tasks_assignee_bot_idx", "assignee_bot_id", "status"),
        Index("work_tasks_reviewer_idx", "reviewer_user_id"),
        Index("work_tasks_principal_idx", "principal_user_id"),
        Index("work_tasks_requester_idx", "requester_user_id"),
        Index("work_tasks_parent_idx", "parent_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(
        Text, server_default=text("('T-'::text || nextval('work_task_code_seq'::regclass))")
    )
    title: Mapped[str] = mapped_column(Text)
    # 给人看的一句话：分派时写要做什么，完成时改成结果。
    summary: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default=text("'dispatch'"))
    # 来源群（网页上建的任务没有群；人派给 AI 员工开工后是代表人和它的私聊）。
    platform: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    origin_event_id: Mapped[int | None] = mapped_column(BigInteger)
    requester_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    requester_name: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # 发起人是 AI 员工时（拆出的子任务）。
    requester_bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="SET NULL")
    )
    # 代表人：AI 员工以谁的身份做事（业务令牌、白名单、个人工具都按这个人）。整条任务链沿用。
    principal_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    # 交付后要不要人验收（AI 员工执行、人发起的任务默认要）。
    review_required: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
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
    root_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("work_tasks.id", ondelete="SET NULL")
    )
    depth: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    related_codes: Mapped[list[str]] = mapped_column(ARRAY(Text()), server_default=text("'{}'"))
    # 给执行员工的要求（Agent 层）：背景、验收标准、注意事项。
    brief: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # 完成时的任务记要（Agent 层），后续相关任务的背景。
    digest: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # 有子任务的任务交付时的完整汇报（Agent 层，人展开看）。
    report: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # 产出物（人话线）：[{title, url}]。
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    # 要不要在群里跟进：简单的事照样记下，但群里不出编号、调度员不发言，执行员工直接回复。
    tracked: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # 在等什么（人话线）：{kind: human|queue|quota|task|external, user_id?, human_text}。
    waiting_on: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 当前待选择事项：网页和飞书卡片上同一份，任一处先回答即生效。见 core/work_tasks/asks.py。
    ask: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    priority: Mapped[str] = mapped_column(Text, server_default=text("'normal'"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # auto：发起时就有人在等结果，交出去就开工；manual：人派给 AI 员工的，等人叫它或定时领取。
    start_mode: Mapped[str] = mapped_column(Text, server_default=text("'auto'"))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class WorkTaskRun(Base):
    """AI 员工跑的一轮：挂在任务下面，不顶替任务状态。人话线只显示「运行中」，细节在这里。"""

    __tablename__ = "work_task_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','done','confirm','failed','cancelled','waiting')",
            name="status",
        ),
        UniqueConstraint("chat_task_id", name="uq_work_task_runs_chat_task_id"),
        Index("work_task_runs_task_idx", "work_task_id", "started_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    work_task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("work_tasks.id", ondelete="CASCADE")
    )
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="SET NULL")
    )
    principal_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    chat_task_id: Mapped[int] = mapped_column(BigInteger)
    session_key: Mapped[str] = mapped_column(Text)
    # dispatch / chat（人在对话里叫它）/ cron（定时领取）/ rerun（验收退回、补充）/ api
    trigger: Mapped[str] = mapped_column(Text)
    triggered_by: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    outcome: Mapped[str] = mapped_column(Text, server_default=text("''"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkTaskWatcher(Base):
    __tablename__ = "work_task_watchers"
    work_task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("work_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
