"""定时任务、执行留痕与人工求助；投递仍复用 outbox。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
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
    Numeric,
    SmallInteger,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin, enum_check

CRON_STATUSES = ("running", "success", "failed", "skipped", "failed_precheck")
ESCALATION_STATUSES = ("pending", "queued", "replied", "completed", "expired", "cancelled")


class CronJob(TimestampMixin, Base):
    __tablename__ = "cron_jobs"
    __table_args__ = (
        CheckConstraint(
            "execution_mode IN ('ai', 'self_reminder', 'personal_ai')", name="execution_mode"
        ),
        CheckConstraint("schedule_kind IN ('recurring', 'once')", name="schedule_kind"),
        CheckConstraint("schedule_kind != 'once' OR run_at IS NOT NULL", name="once_run_at"),
        CheckConstraint("precheck_timeout_seconds BETWEEN 5 AND 120", name="precheck_timeout"),
        Index("cron_jobs_due_idx", "next_run_at", postgresql_where=text("enabled = true")),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    execution_mode: Mapped[str] = mapped_column(Text, server_default=text("'ai'"))
    reminder_chat_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    cron_expression: Mapped[str] = mapped_column(Text)
    schedule_kind: Mapped[str] = mapped_column(Text, server_default=text("'recurring'"))
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(Text, server_default=text("'Asia/Shanghai'"))
    prompt: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    precheck_script: Mapped[str | None] = mapped_column(Text)
    precheck_timeout_seconds: Mapped[int] = mapped_column(Integer, server_default=text("30"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target_users: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), server_default=text("'{}'"))
    target_chats: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    notify_emails: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    notify_webhook: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    notify_webhook_url_enc: Mapped[str | None] = mapped_column(Text, comment="enc")
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    force_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    force_run_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    running_task_id: Mapped[int | None] = mapped_column(BigInteger)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class CronRun(Base):
    __tablename__ = "cron_runs"
    __table_args__ = (
        CheckConstraint(enum_check("status", CRON_STATUSES), name="status"),
        Index("cron_runs_job_idx", "cron_job_id", text("id DESC")),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    cron_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cron_jobs.id", ondelete="SET NULL")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_name: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    executed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    trigger_kind: Mapped[str] = mapped_column(Text, server_default=text("'scheduled'"))
    status: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    precheck_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    delivery: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_creation_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 以任务创建者本人身份运行过、或属于本人专属任务：指令与结果只给本人看。
    private: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class Escalation(TimestampMixin, Base):
    __tablename__ = "escalations"
    __table_args__ = (
        CheckConstraint(enum_check("status", ESCALATION_STATUSES), name="status"),
        CheckConstraint(
            "resolution IN ('agent','observed','offline','expired')", name="resolution"
        ),
        CheckConstraint("rounds BETWEEN 0 AND 2", name="rounds"),
        CheckConstraint("nudge_stage BETWEEN 0 AND 2", name="nudge_stage"),
        CheckConstraint("notify_platform IN ('wecom_app','feishu_bot')", name="notify_platform"),
        Index("escalations_owner_group_idx", "owner_client_key", "group_id"),
        Index(
            "escalations_one_active_recipient",
            "to_user_id",
            unique=True,
            postgresql_where=text("status IN ('pending','replied')"),
        ),
        Index(
            "escalations_active_idx",
            "to_user_id",
            "status",
            postgresql_where=text("status IN ('pending','replied','queued')"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    escalation_id: Mapped[str] = mapped_column(Text, unique=True)
    owner_client_key: Mapped[str] = mapped_column(Text, ForeignKey("api_clients.app_key"))
    request_fingerprint: Mapped[str] = mapped_column(Text)
    from_platform_user_id: Mapped[str | None] = mapped_column(Text)
    to_platform_user_id: Mapped[str] = mapped_column(Text)
    followup_questions: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    group_id: Mapped[str | None] = mapped_column(Text)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id"))
    from_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    to_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    notify_platform: Mapped[str] = mapped_column(Text)
    notify_message_id: Mapped[str | None] = mapped_column(Text)
    # 固定应用，禁止回调从同平台的其它应用串入；外键保留历史配置的归属。
    platform_app_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("platform_apps.id"))
    question: Mapped[str] = mapped_column(Text)
    replies: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(Text)
    rounds: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    nudge_stage: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    resolution: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
