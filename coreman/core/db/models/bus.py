"""运行时总线表（spec §5.4）。所有进程只经这些表 + pg_notify 通信。"""

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
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from coreman.core.db.base import Base, enum_check

CONNECTION_STATES = ("disconnected", "connecting", "subscribed", "kicked", "auth_failed")
INBOUND_KINDS = ("message", "card_action", "enter_chat", "feedback", "bot_added")
TASK_KINDS = (
    "chat",
    "command",
    "card_action",
    "choice_submit",
    "cron_run",
    "relay_switch",
    "escalation_media",
    "skill_install",
)
TASK_LANES = ("normal", "fast")
TASK_STATUSES = ("queued", "claimed", "running", "succeeded", "failed", "cancelled", "timed_out")
DELIVERY_MODES = ("stream", "proactive")
OUTBOX_KINDS = ("send", "card_update", "welcome", "stream_finish", "notify")
OUTBOX_STATUSES = ("pending", "sending", "sent", "failed", "skipped")


def _ts(**kw: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class ProcessInstance(Base):
    """网关/工作进程的活体登记；id 形如 `<service>-<node>:<host>:<pid>:<boot>`。"""

    __tablename__ = "process_instances"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    service: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = _ts()
    heartbeat_at: Mapped[datetime] = _ts()
    capacity: Mapped[int | None] = mapped_column(Integer)
    running: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    drain_requested_at: Mapped[datetime | None] = _ts()
    stopped_at: Mapped[datetime | None] = _ts()


class BotLease(Base):
    """一个 bot 同时只能被一个网关实例持有；generation 每次续租/抢占单调递增。"""

    __tablename__ = "bot_leases"
    __table_args__ = (
        CheckConstraint(enum_check("connection_state", CONNECTION_STATES), name="connection_state"),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    platform: Mapped[str] = mapped_column(Text)
    holder_instance: Mapped[str | None] = mapped_column(
        Text, ForeignKey("process_instances.id", ondelete="SET NULL")
    )
    generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    acquired_at: Mapped[datetime | None] = _ts()
    heartbeat_at: Mapped[datetime | None] = _ts()
    drain_requested_by: Mapped[str | None] = mapped_column(Text)
    drain_requested_at: Mapped[datetime | None] = _ts()
    released_at: Mapped[datetime | None] = _ts()
    connection_state: Mapped[str] = mapped_column(Text, server_default=text("'disconnected'"))


class InboundEvent(Base):
    """平台回调落库后的唯一事实；(bot_id, platform_msg_id) 唯一做平台重投去重。"""

    __tablename__ = "inbound_events"
    __table_args__ = (
        CheckConstraint(enum_check("chat_type", ("single", "group")), name="chat_type"),
        CheckConstraint(enum_check("kind", INBOUND_KINDS), name="kind"),
        UniqueConstraint("bot_id", "platform_msg_id", name="uq_inbound_events_bot_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(Text)
    platform_msg_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    chat_type: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(Text)
    sender_platform_user_id: Mapped[str | None] = mapped_column(Text)
    sender_open_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reply_context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = _ts(server_default=text("now()"))


class Task(Base):
    """工作队列；worker 用 tasks_claim_idx + SKIP LOCKED 抢单。"""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(enum_check("lane", TASK_LANES), name="lane"),
        CheckConstraint(enum_check("status", TASK_STATUSES), name="status"),
        Index(
            "tasks_claim_idx",
            "lane",
            text("priority DESC"),
            "id",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "tasks_active_session_idx",
            "bot_id",
            "session_key",
            postgresql_where=text("status IN ('claimed','running')"),
        ),
        Index(
            "tasks_finished_idx", "finished_at", postgresql_where=text("finished_at IS NOT NULL")
        ),
        Index("tasks_heartbeat_idx", "heartbeat_at", postgresql_where=text("status = 'running'")),
        # 删入站事件时外键检查按 inbound_event_id 反查任务；没有它每删一行扫一遍 tasks。
        Index(
            "tasks_inbound_event_idx",
            "inbound_event_id",
            postgresql_where=text("inbound_event_id IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    lane: Mapped[str] = mapped_column(Text, server_default=text("'normal'"))
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    session_key: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    inbound_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("inbound_events.id")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    run_after: Mapped[datetime] = _ts(server_default=text("now()"))
    claimed_by: Mapped[str | None] = mapped_column(
        Text, ForeignKey("process_instances.id", ondelete="SET NULL")
    )
    claimed_at: Mapped[datetime | None] = _ts()
    started_at: Mapped[datetime | None] = _ts()
    finished_at: Mapped[datetime | None] = _ts()
    heartbeat_at: Mapped[datetime | None] = _ts()
    cancel_requested_at: Mapped[datetime | None] = _ts()
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)


class TaskStream(Base):
    """一次流式回复的可恢复状态；随 task 级联删除。"""

    __tablename__ = "task_streams"
    __table_args__ = (
        CheckConstraint(enum_check("delivery_mode", DELIVERY_MODES), name="delivery_mode"),
        Index(
            "task_streams_active_idx",
            "bot_id",
            postgresql_where=text("is_complete = false OR finish_pushed_at IS NULL"),
        ),
    )
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    platform: Mapped[str] = mapped_column(Text)
    stream_id: Mapped[str] = mapped_column(Text)
    reply_context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    lease_generation: Mapped[int] = mapped_column(BigInteger)
    delivery_mode: Mapped[str] = mapped_column(Text, server_default=text("'stream'"))
    thinking_md: Mapped[str] = mapped_column(Text, server_default=text("''"))
    pending_text: Mapped[str] = mapped_column(Text, server_default=text("''"))
    final_text: Mapped[str | None] = mapped_column(Text)
    pending_card: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    session_url: Mapped[str | None] = mapped_column(Text)
    segment_boundaries: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), server_default=text("'{}'")
    )
    running_since: Mapped[datetime] = _ts()
    is_complete: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    completed_at: Mapped[datetime | None] = _ts()
    version: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    pushed_version: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    finish_pushed_at: Mapped[datetime | None] = _ts()
    background_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))


class OutboxItem(Base):
    """出站消息队列；dedupe_key 唯一保证「至少一次投递 + 平台侧幂等」。"""

    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(enum_check("kind", OUTBOX_KINDS), name="kind"),
        CheckConstraint(enum_check("status", OUTBOX_STATUSES), name="status"),
        CheckConstraint(
            "(kind = 'notify' AND bot_id IS NULL) OR (kind != 'notify' AND bot_id IS NOT NULL)",
            name="target_kind",
        ),
        Index(
            "outbox_pending_idx",
            "bot_id",
            "not_before",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
        # 认领语句的外层与同组 NOT EXISTS、scheduler 回收 sending：只看还没落定的这一小撮行。
        Index(
            "outbox_active_idx",
            "bot_id",
            "id",
            postgresql_where=text("status IN ('pending','sending')"),
        ),
        # 失败排查、告警计数、失败 / 跳过条目的保留期清理。
        Index(
            "outbox_failed_idx",
            "bot_id",
            "id",
            postgresql_where=text("status IN ('failed','skipped')"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE")
    )
    platform: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    target: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    lease_generation: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    not_before: Mapped[datetime] = _ts(server_default=text("now()"))
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))


class ChatSession(Base):
    """(bot, 会话键) → relay 会话；session_key 单聊是 user_id、群聊是 chat_id。"""

    __tablename__ = "chat_sessions"
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    session_key: Mapped[str] = mapped_column(Text, primary_key=True)
    relay_session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    backend: Mapped[str] = mapped_column(Text)
    last_speaker_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    last_active_at: Mapped[datetime] = _ts(server_default=text("now()"))


class UserReached(Base):
    """主动推送（cron/webhook）的前置条件：这个用户跟这个 bot 说过话。"""

    __tablename__ = "user_reached"
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    platform_chat_id: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = _ts(server_default=text("now()"))
