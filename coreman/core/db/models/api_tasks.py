"""通过 API 驱动 AI 员工：个人令牌、API 任务、群别名。

见 docs/superpowers/specs/2026-11-06-api-driven-tasks.md。任务状态不另存：查询时读总线任务和
对话记录，这里只记谁、用哪枚令牌、在哪个会话里发起了哪件事。
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
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, enum_check

API_TASK_TARGETS = ("p2p", "chat")


def _ts(**kw: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class PersonalAccessToken(Base):
    """成员自己建的 API 令牌：任务以所有人本人的身份运行，只能驱动 `bot_ids` 里的 AI 员工。"""

    __tablename__ = "personal_access_tokens"
    __table_args__ = (Index("personal_access_tokens_user_idx", "user_id", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    # 明文的前几位，列表里给人认是哪一枚；库里只存哈希。
    token_prefix: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    bot_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid()))
    expires_at: Mapped[datetime] = _ts()
    last_used_at: Mapped[datetime | None] = _ts()
    revoked_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))


class ApiTask(Base):
    """一次 API 发起：对应总线上的一个 chat 任务。"""

    __tablename__ = "api_tasks"
    __table_args__ = (
        CheckConstraint(enum_check("target", API_TASK_TARGETS), name="target"),
        UniqueConstraint("token_id", "request_id", name="uq_api_tasks_token_request"),
        Index("api_tasks_token_idx", "token_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_access_tokens.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    request_id: Mapped[str | None] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text)
    alias: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))


class BotConversationAlias(Base):
    """群别名：群成员在群里绑定，API 用别名指定在哪个群承接，不必知道飞书的 chat_id。"""

    __tablename__ = "bot_conversation_aliases"
    __table_args__ = (
        UniqueConstraint("bot_id", "alias", name="uq_bot_conversation_aliases_alias"),
        UniqueConstraint("bot_id", "chat_id", name="uq_bot_conversation_aliases_chat"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
