"""机器人：bots、bot_members、bot_allowed_users。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin
from coreman.core.db.models.account import PLATFORM_CHECK

BOT_KEY_RE = r"^[a-z0-9][a-z0-9_-]{1,49}$"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh")


class Bot(TimestampMixin, Base):
    __tablename__ = "bots"
    __table_args__ = (
        CheckConstraint(f"bot_key ~ '{BOT_KEY_RE}'", name="bot_key"),
        CheckConstraint(PLATFORM_CHECK, name="platform"),
        CheckConstraint("verbosity_level BETWEEN 1 AND 4", name="verbosity_level"),
        CheckConstraint("effort_level IN ('low','medium','high','xhigh')", name="effort_level"),
        CheckConstraint("sse_timeout_seconds BETWEEN 1800 AND 43200", name="sse_timeout_seconds"),
        # 唯一约束在这里给全名（不再在列上写 unique=True），保证 alembic check 只看到一个同名约束。
        UniqueConstraint("bot_key", name="uq_bots_bot_key"),
        Index("bots_relay_idx", "relay_server_id"),
        # 早先漏建：这两列都是外键，级联删除与「我创建的机器人」列表都要走它们（迁移 0004 补）。
        Index("bots_team_id_idx", "team_id"),
        Index("bots_created_by_idx", "created_by"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    bot_key: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("teams.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    relay_server_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("relay_servers.id"))
    model: Mapped[str] = mapped_column(Text)
    working_dir: Mapped[str] = mapped_column(Text)
    workspace_state: Mapped[str] = mapped_column(Text, server_default=text("'ready'"))
    workspace_phase: Mapped[str | None] = mapped_column(Text)
    workspace_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memory_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workspace_error: Mapped[str | None] = mapped_column(Text)
    workspace_operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    workspace_target_relay_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    workspace_target_dir: Mapped[str | None] = mapped_column(Text)
    git_url: Mapped[str | None] = mapped_column(Text)
    git_branch: Mapped[str] = mapped_column(Text, server_default=text("'main'"))
    git_token_enc: Mapped[str] = mapped_column(Text, server_default=text("''"), comment="enc")
    git_last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    system_prompt: Mapped[str] = mapped_column(Text, server_default=text("''"))
    merged_system_prompt: Mapped[str] = mapped_column(Text, server_default=text("''"))
    verbosity_level: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    effort_level: Mapped[str | None] = mapped_column(Text)
    sse_timeout_seconds: Mapped[int] = mapped_column(Integer, server_default=text("3600"))
    # 已不再读取，下个版本删除：IM 投递时限由平台策略决定（worker 不读此列），
    # 列暂留只为兼容上一版本代码对 bots 表的读写。
    agent_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    credentials_enc: Mapped[str] = mapped_column(Text, comment="enc")
    env_vars_enc: Mapped[str] = mapped_column(Text, server_default=text("''"), comment="enc")
    welcome_message: Mapped[str | None] = mapped_column(Text)
    notify_webhook_url: Mapped[str | None] = mapped_column(Text)
    # 已不再读写，下个版本删除：不通过 API 配置或展示，列暂留只为兼容现有数据库。
    custom_command_modules: Mapped[list[str]] = mapped_column(
        ARRAY(Text()), server_default=text("'{}'::text[]")
    )
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    # 乐观锁，语义同 PlatformApp.version。
    __mapper_args__ = {"version_id_col": version}


class BotMember(Base):
    """bot 管理员；创建者不入此表。"""

    __tablename__ = "bot_members"
    __table_args__ = (
        CheckConstraint("role IN ('admin')", name="role"),
        PrimaryKeyConstraint("bot_id", "user_id"),
        # 复合主键的前缀是 bot_id，按 user_id 反查（删用户、查「我管的机器人」）没有索引可用。
        Index("bot_members_user_id_idx", "user_id"),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(Text, server_default=text("'admin'"))
    added_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class BotAllowedUser(Base):
    """bot 白名单；空表 = 不限制。"""

    __tablename__ = "bot_allowed_users"
    __table_args__ = (PrimaryKeyConstraint("bot_id", "user_id"),)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
