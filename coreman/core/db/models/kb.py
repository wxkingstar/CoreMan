"""调度员知识库：从白名单来源抽出的事实条目，以及每次抽取与编译的运行记录。

见 docs/superpowers/specs/2026-11-28-dispatcher-knowledge-base.md。条目是脱敏后的事实，不带
原话；Wiki 里每句知识用 `K-<id>` 指回条目，条目再指回来源。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    Uuid,
)
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, enum_check

KNOWLEDGE_KINDS = ("system", "role", "bot", "decision", "process", "term")
KNOWLEDGE_CONFIDENCES = ("stated", "inferred")
KNOWLEDGE_SOURCES = ("task", "rules", "catalog", "roster", "group")
KNOWLEDGE_ITEM_STATUSES = ("pending", "compiled", "dropped", "purged")
KNOWLEDGE_RUN_MODES = ("daily", "cold_start", "purge")
KNOWLEDGE_RUN_STATUSES = ("running", "ok", "skipped", "needs_review", "failed")


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"
    __table_args__ = (
        CheckConstraint(enum_check("kind", KNOWLEDGE_KINDS), name="kind"),
        CheckConstraint(enum_check("confidence", KNOWLEDGE_CONFIDENCES), name="confidence"),
        CheckConstraint(enum_check("source_kind", KNOWLEDGE_SOURCES), name="source_kind"),
        CheckConstraint(enum_check("status", KNOWLEDGE_ITEM_STATUSES), name="status"),
        Index("knowledge_items_bot_status_idx", "bot_id", "status", "id"),
        Index("knowledge_items_source_idx", "source_kind", "source_ref"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    occurred_on: Mapped[date | None] = mapped_column(Date)
    confidence: Mapped[str] = mapped_column(Text, server_default=sql("'stated'"))
    source_kind: Mapped[str] = mapped_column(Text)
    # 任务编号、员工标识加文件路径、系统标识；写进 Wiki 的来源标记由它和 `K-<id>` 拼出。
    source_ref: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=sql("'pending'"))
    # 规则过滤命中的规则名，或 `not_compiled`（几次编译都没有落到任何一页）。
    dropped_reason: Mapped[str | None] = mapped_column(Text)
    # 交给编译回合的次数：超过上限仍没落页的不再重试。
    attempts: Mapped[int] = mapped_column(Integer, server_default=sql("0"))
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge_runs.id", ondelete="SET NULL")
    )
    compiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql("now()")
    )


class KnowledgeRun(Base):
    __tablename__ = "knowledge_runs"
    __table_args__ = (
        CheckConstraint(enum_check("mode", KNOWLEDGE_RUN_MODES), name="mode"),
        CheckConstraint(enum_check("status", KNOWLEDGE_RUN_STATUSES), name="status"),
        Index("knowledge_runs_started_idx", "started_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=sql("'running'"))
    # 实际用的后端与模型；没选出后端（跳过）时为空。
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    batches: Mapped[int] = mapped_column(Integer, server_default=sql("0"))
    failed_batches: Mapped[int] = mapped_column(Integer, server_default=sql("0"))
    extracted: Mapped[int] = mapped_column(Integer, server_default=sql("0"))
    dropped: Mapped[int] = mapped_column(Integer, server_default=sql("0"))
    compiled: Mapped[int] = mapped_column(Integer, server_default=sql("0"))
    # 各来源的批次数、丢弃原因分布、编译后检查的命中等，给管理员看。
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sql("'{}'::jsonb"))
    # 编译回合自己写的变更摘要。
    summary: Mapped[str] = mapped_column(Text, server_default=sql("''"))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
