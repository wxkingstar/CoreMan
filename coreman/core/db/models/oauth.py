"""CoreMan 作为 OAuth 授权服务器：动态注册的本机客户端、成员的授权与令牌。

见 docs/superpowers/specs/2026-11-25-coreman-oauth-mcp-cli.md。令牌只存 SHA-256。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY, INET
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base


def _ts(**kw: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class OAuthClient(Base):
    """动态注册的公开客户端（本机 Agent、命令行）；回调只能是本机回环地址。"""

    __tablename__ = "oauth_clients"
    __table_args__ = (Index("oauth_clients_ip_idx", "created_ip", "created_at"),)
    client_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # 客户端自报的名字，确认页上标明「自报」。
    client_name: Mapped[str] = mapped_column(Text)
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(Text()))
    created_ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    last_used_at: Mapped[datetime | None] = _ts()


class OAuthGrant(Base):
    """成员对一个客户端的一次授权；续期令牌每次轮换，旧的一代再出现就整次吊销。"""

    __tablename__ = "oauth_grants"
    __table_args__ = (Index("oauth_grants_user_idx", "user_id", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    client_id: Mapped[str] = mapped_column(
        Text, ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(Text)
    refresh_hash: Mapped[str] = mapped_column(Text, unique=True)
    # 上一代续期令牌：被重放说明泄露了。
    previous_refresh_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    refresh_expires_at: Mapped[datetime] = _ts()
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    last_used_at: Mapped[datetime | None] = _ts()
    revoked_at: Mapped[datetime | None] = _ts()


class OAuthAccessToken(Base):
    __tablename__ = "oauth_access_tokens"
    __table_args__ = (Index("oauth_access_tokens_expiry_idx", "expires_at"),)
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    grant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("oauth_grants.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = _ts()
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
