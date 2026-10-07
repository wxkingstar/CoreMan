"""系统授权、调用方凭证与 ES256 密钥。"""

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
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin, enum_check

TOKEN_DELIVERIES = ("env", "proxy")


class BusinessSystem(TimestampMixin, Base):
    __tablename__ = "systems"
    __table_args__ = (
        CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,49}$'", name="key_format"),
        CheckConstraint(enum_check("token_delivery", TOKEN_DELIVERIES), name="token_delivery"),
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)
    # 业务系统按接入规范提供的 OpenAPI 描述地址，与 base_url 同源。
    openapi_url: Mapped[str | None] = mapped_column(Text)
    # 已改名为 openapi_url：保留一个版本给滚动升级中的旧进程读，写入时与 openapi_url 同值。
    sitemap_url: Mapped[str | None] = mapped_column(Text)
    # env：令牌按 BOT_TOKEN_<KEY> 下发到运行环境；proxy：由平台代理调用，令牌不进运行环境。
    token_delivery: Mapped[str] = mapped_column(Text, server_default=text("'env'"))
    token_provider: Mapped[str] = mapped_column(Text, server_default=text("'builtin'"))
    token_audience: Mapped[str | None] = mapped_column(Text)
    access_test_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    default_for_all_bots: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    allowed_bot_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class BotSystemGrant(Base):
    __tablename__ = "bot_system_grants"
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    system_key: Mapped[str] = mapped_column(
        Text, ForeignKey("systems.key", ondelete="CASCADE"), primary_key=True
    )
    # 平台代理调用（token_delivery=proxy）时是否允许 write 级操作；
    # destructive、financial 一律不开放。
    allow_write: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    granted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class SystemGrantAudit(Base):
    """已弃用：原先只写不读，现已停止写入，授权变更以 audit_logs 的 bot.system_grants /
    system.update 为准。映射只为让模型与迁移保持一致（alembic check），不从 models 包导出、
    不得再读写；按 expand-only 规则表本身保留，下个版本随迁移一并删除。"""

    __tablename__ = "system_grant_audit"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    requested: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    approved: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


CATALOG_STATUSES = ("ok", "stale", "error")


class SystemCatalog(Base):
    """业务系统的操作目录：拉取到的 OpenAPI 描述编译后的结果，按系统一行。

    `compiled` 是编译后的目录（见 core/systems_catalog/compiler.py），不保存描述原文；
    `compiler_version` 落后于当前编译器时，下次复查不带 If-None-Match 重新拉取并编译。
    """

    __tablename__ = "system_catalogs"
    __table_args__ = (CheckConstraint(enum_check("status", CATALOG_STATUSES), name="status"),)
    system_key: Mapped[str] = mapped_column(
        Text, ForeignKey("systems.key", ondelete="CASCADE"), primary_key=True
    )
    spec_url: Mapped[str] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    spec_sha256: Mapped[str | None] = mapped_column(Text)
    spec_bytes: Mapped[int | None] = mapped_column(Integer)
    # 最近一次拿到新内容、最近一次复查（含 304 与失败）。
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    operation_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    hidden_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    module_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lint: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    compiled: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    compiler_version: Mapped[int] = mapped_column(Integer, server_default=text("0"))


TOKEN_ISSUE_PURPOSES = ("chat", "cron", "health", "access_test", "catalog", "proxy")


class BusinessTokenIssue(Base):
    """每次为业务系统签发的令牌：只记令牌标识和签发上下文，不记令牌本身。

    业务系统日志里的 token_id（JWT 的 jti）按它查回是哪个 AI 员工、哪一轮任务、替谁签的。
    不加外键：机器人、用户删除后签发记录仍要能追溯。
    """

    __tablename__ = "business_token_issues"
    __table_args__ = (
        CheckConstraint(enum_check("purpose", TOKEN_ISSUE_PURPOSES), name="purpose"),
        Index(
            "business_token_issues_token_idx",
            "token_id",
            postgresql_where=text("token_id IS NOT NULL"),
        ),
        Index("business_token_issues_task_idx", "task_id"),
        Index("business_token_issues_time_idx", "issued_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    purpose: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    subject: Mapped[str] = mapped_column(Text)
    system_key: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(Text)
    # JWT 的 jti；外部签发方返回不透明令牌时为空。
    token_id: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SystemCall(Base):
    """平台代理的一次业务系统调用（含被策略拒绝的）：只记元数据，不记参数、响应和令牌。

    `token_id` 是代理令牌的 jti，与 business_token_issues 对账；
    不加外键，机器人、用户删除后仍可追溯。
    """

    __tablename__ = "system_calls"
    __table_args__ = (
        Index("system_calls_task_idx", "task_id"),
        Index("system_calls_time_idx", "called_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    system_key: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(Text)
    # ok / http_error / denied / invalid / unreachable
    outcome: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    token_id: Mapped[str | None] = mapped_column(Text)


class ApiClient(TimestampMixin, Base):
    __tablename__ = "api_clients"
    app_key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    secret_enc: Mapped[str] = mapped_column(Text, comment="enc")
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}


class JwtKey(Base):
    __tablename__ = "jwt_keys"
    __table_args__ = (
        Index(
            "jwt_keys_one_active_idx", "is_active", unique=True, postgresql_where=text("is_active")
        ),
    )
    kid: Mapped[str] = mapped_column(Text, primary_key=True)
    private_pem_enc: Mapped[str] = mapped_column(Text, comment="enc")
    public_jwk: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
