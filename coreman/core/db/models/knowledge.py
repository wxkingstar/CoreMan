"""技能目录、安装审批与机器人记忆。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin


class SkillSource(TimestampMixin, Base):
    __tablename__ = "skill_sources"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str] = mapped_column(Text)
    git_url: Mapped[str | None] = mapped_column(Text)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class Skill(TimestampMixin, Base):
    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint("security_level IN ('public','internal')", name="security_level"),
        CheckConstraint("install_type IN ('git','mcp')", name="install_type"),
        Index("skills_source_idx", "source_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("skill_sources.id"))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    category: Mapped[str | None] = mapped_column(Text)
    security_level: Mapped[str] = mapped_column(Text, server_default=text("'public'"))
    version: Mapped[str | None] = mapped_column(Text)
    env_groups: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    selectable_env_groups: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    data_sources: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    default_data_source: Mapped[str | None] = mapped_column(Text)
    doris_enabled_groups: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'")
    )
    user_env_vars: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    install_type: Mapped[str] = mapped_column(Text, server_default=text("'git'"))
    external_repo_url: Mapped[str | None] = mapped_column(Text)
    mcp_config_enc: Mapped[str | None] = mapped_column(Text)
    security_prompt_template: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": revision}


class EnvPreset(TimestampMixin, Base):
    __tablename__ = "env_presets"
    group_key: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    vars_enc: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class BotSkill(TimestampMixin, Base):
    __tablename__ = "bot_skills"
    __table_args__ = (
        CheckConstraint(
            "status IN ('installed','pending_approval','installing','failed','uninstalled')",
            name="status",
        ),
        Index("bot_skills_skill_idx", "skill_id"),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("skills.id"), primary_key=True)
    status: Mapped[str] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selected_env_groups: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    user_env_vars_enc: Mapped[str | None] = mapped_column(Text)
    effective_env_enc: Mapped[str | None] = mapped_column(Text)
    security_prompt: Mapped[str | None] = mapped_column(Text)
    approved_databases: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    install_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    error_message: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": revision}


class SkillApproval(Base):
    __tablename__ = "skill_approvals"
    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','rejected','withdrawn')", name="status"),
        Index(
            "skill_approvals_pending_idx",
            "requested_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "skill_approvals_one_pending",
            "bot_id",
            "skill_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("skills.id"))
    requested_databases: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    requested_security_prompt: Mapped[str] = mapped_column(Text)
    reinstall_code: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # 审批绑定目录内容版本；目录改变后不能借旧审批安装新实现或配置。
    skill_revision: Mapped[int] = mapped_column(Integer)
    request_inputs_enc: Mapped[str] = mapped_column(Text)
    approved_databases: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    approved_security_prompt: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class Memory(TimestampMixin, Base):
    __tablename__ = "memories"
    __table_args__ = (UniqueConstraint("bot_id", "file_name"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    file_name: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    collected_from: Mapped[str | None] = mapped_column(Text)
    file_mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 删除留墓碑，避免旧实例下一次自动回收把已删内容复活。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}
