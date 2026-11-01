"""飞书群聊旁听：每个群的防抖与静音状态，以及每次判断的记录（见 docs/feishu.md「群聊旁听」）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base

GROUP_OBSERVE_MODES = ("off", "shadow", "on")
DECISION_REVIEWS = ("should", "should_not")


class GroupObserveState(Base):
    """一个机器人在一个群里的旁听状态。

    `pending_since` 非空表示有没判断过的新消息；`bot_streak` 是最近一条人类消息之后的
    机器人消息数（含本机器人自己说的），人一说话就清零。
    """

    __tablename__ = "group_observe_states"
    __table_args__ = (PrimaryKeyConstraint("bot_id", "chat_id"),)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(Text)
    pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_id: Mapped[int | None] = mapped_column(BigInteger)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bot_streak: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_interjected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GroupObserveDecision(Base):
    """一次旁听判断：跳过的原因或模型给的概率、是否插话，以及管理员的复核标注。"""

    __tablename__ = "group_observe_decisions"
    __table_args__ = (
        CheckConstraint("mode IN ('shadow','on')", name="mode"),
        CheckConstraint("review IS NULL OR review IN ('should','should_not')", name="review"),
        Index("group_observe_decisions_bot_idx", "bot_id", "created_at"),
        Index("group_observe_decisions_created_idx", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(Text)
    event_id: Mapped[int | None] = mapped_column(BigInteger)
    mode: Mapped[str] = mapped_column(Text)
    # interject / below_threshold / refused / error / busy / muted / bot_streak / vetoed ...
    outcome: Mapped[str] = mapped_column(Text)
    probability: Mapped[float | None] = mapped_column(Float)
    model: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    # 交给判断模型的输入（群消息摘录），复核时展开看。
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    review: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
