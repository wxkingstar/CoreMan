"""账号与组织（spec §5.1）+ 登录辅助表 auth_nonces / login_attempts。"""

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
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coreman.core.db.base import Base, TimestampMixin

PLATFORM_CHECK = "platform IN ('wecom','feishu')"


class Team(TimestampMixin, Base):
    __tablename__ = "teams"
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name_zh: Mapped[str] = mapped_column(Text)
    name_ja: Mapped[str | None] = mapped_column(Text)
    name_en: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class TeamRule(TimestampMixin, Base):
    """部门路径子串 → 团队，按 sort_order 先命中先返回。"""

    __tablename__ = "team_rules"
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    team_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("teams.id", ondelete="CASCADE"))
    platform: Mapped[str | None] = mapped_column(Text)
    dept_path_contains: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="status"),
        CheckConstraint("locale IN ('zh','ja','en')", name="locale"),
        CheckConstraint(
            "role IN ('platform_admin','ai_committee','team_lead','member')", name="role"
        ),
        CheckConstraint("source IN ('sync','bootstrap','manual')", name="source"),
        Index(
            "users_email_uk",
            func.lower(text("email")),
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index(
            "users_mobile_uk", "mobile", unique=True, postgresql_where=text("mobile IS NOT NULL")
        ),
        Index(
            "users_bootstrap_uk",
            "source",
            unique=True,
            postgresql_where=text("source = 'bootstrap'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    login_name: Mapped[str | None] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    mobile: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    locale: Mapped[str] = mapped_column(Text, server_default=text("'zh'"))
    role: Mapped[str] = mapped_column(Text, server_default=text("'member'"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("teams.id"))
    position: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[str | None] = mapped_column(Text)
    bot_accessible: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    manual_fields: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(Text, server_default=text("'sync'"))
    identities: Mapped[list[UserIdentity]] = relationship(
        "UserIdentity", back_populates="user", cascade="all, delete-orphan"
    )


class UserIdentity(TimestampMixin, Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        CheckConstraint(PLATFORM_CHECK, name="platform"),
        UniqueConstraint("platform", "platform_user_id", name="uq_user_identities_platform"),
        Index("user_identities_open_id_idx", "platform", "open_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(Text)
    platform_user_id: Mapped[str] = mapped_column(Text)
    open_id: Mapped[str | None] = mapped_column(Text)
    union_id: Mapped[str | None] = mapped_column(Text)
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user: Mapped[User] = relationship("User", back_populates="identities")


class Department(TimestampMixin, Base):
    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("platform", "platform_dept_id", name="uq_departments_platform"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    platform: Mapped[str] = mapped_column(Text)
    platform_dept_id: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("departments.id"))
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class UserDepartment(Base):
    __tablename__ = "user_departments"
    __table_args__ = (PrimaryKeyConstraint("user_id", "department_id"),)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    department_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("departments.id", ondelete="CASCADE")
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    auth_method: Mapped[str] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    user: Mapped[User] = relationship(User, lazy="selectin")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("audit_logs_target_idx", "target_type", "target_id", text("created_at DESC")),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_login: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AuthNonce(Base):
    """OAuth state（单次使用）与已用 code（防重放）。"""

    __tablename__ = "auth_nonces"
    __table_args__ = (PrimaryKeyConstraint("kind", "value"),)
    kind: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (Index("login_attempts_ip_time_idx", "ip", "attempted_at"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ip: Mapped[str] = mapped_column(INET)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
