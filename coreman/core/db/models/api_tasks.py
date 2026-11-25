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
    Integer,
    LargeBinary,
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
        # 发起方二选一：成员的个人令牌，或代成员调用的 API 调用方。
        CheckConstraint("(token_id IS NULL) <> (client_key IS NULL)", name="caller"),
        UniqueConstraint("token_id", "request_id", name="uq_api_tasks_token_request"),
        UniqueConstraint("client_key", "request_id", name="uq_api_tasks_client_request"),
        Index("api_tasks_token_idx", "token_id", "created_at"),
        Index("api_tasks_client_idx", "client_key", "user_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    token_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("personal_access_tokens.id", ondelete="CASCADE")
    )
    client_key: Mapped[str | None] = mapped_column(
        Text, ForeignKey("api_clients.app_key", ondelete="CASCADE")
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
    # 业务系统随任务附带的图片（api_task_uploads.id，按顺序）；图片过期删掉后引用留着。
    image_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid()), server_default=text("'{}'"))
    # 运行时额度用完、排队等恢复：第一次进入等待的时间（满 7 天放弃）与预计重跑时间。
    quota_waiting_since: Mapped[datetime | None] = _ts()
    resume_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))


class ApiTaskUpload(Base):
    """业务系统为某位员工上传的一张图片：任务里按 id 引用。内容直接存在库里，单张不超过 10MB。"""

    __tablename__ = "api_task_uploads"
    __table_args__ = (
        UniqueConstraint(
            "client_key", "user_id", "sha256", name="uq_api_task_uploads_owner_sha256"
        ),
        Index("api_task_uploads_expires_idx", "expires_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    client_key: Mapped[str] = mapped_column(
        Text, ForeignKey("api_clients.app_key", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    sha256: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(Text)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    expires_at: Mapped[datetime] = _ts()


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


class ApiClientGroupRoute(Base):
    """群承接配置：调用方让这个 AI 员工在这个群的主会话里，以固定的服务账号身份跑一轮。

    调用方为某个 AI 员工配了任何一行，驱动它时就只走群模式（请求里不能再指定员工）。
    """

    __tablename__ = "api_client_group_routes"
    __table_args__ = (
        UniqueConstraint("client_key", "bot_id", "chat_id", name="uq_api_client_group_routes_chat"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    client_key: Mapped[str] = mapped_column(
        Text, ForeignKey("api_clients.app_key", ondelete="CASCADE")
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(Text)
    service_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
