"""Business-token providers; external issuance never falls back to local signing."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import quote_plus

import httpx
import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.external_key import ExternalKey
from coreman.core.auth.provider_config import HTTPTokenProviderConfig
from coreman.core.auth.tokens import issue_token, signing_key
from coreman.core.crypto import Cipher
from coreman.core.db.models import BusinessSystem

# 业务系统 key 同时是发言者令牌的 audience。CoreMan 管理 API 自己按 audience="coreman"
# 接受 bot_token（api/bot_auth.py，且免 CSRF）：若允许登记名为 coreman 的系统，平台就会
# 为每位发言者签发一枚能以其身份调用管理 API 的令牌，并注入 bot 管理员可控的 CLI。
PLATFORM_AUDIENCE = "coreman"
# 创建时拒绝；历史库里若已有同名行，签发与授权也一律跳过。
RESERVED_SYSTEM_KEYS = frozenset({PLATFORM_AUDIENCE})
ISSUE_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class TokenRequest:
    subject: str
    audience: str
    ttl_seconds: int


@dataclass(frozen=True)
class IssuedToken:
    value: str = field(repr=False)
    expires_in: int
    expires_at: int
    auth_mode: Literal["cookie", "bearer"]
    # JWT 的 jti，只用于签发记录和追溯；不透明令牌为空。
    token_id: str | None = None


def jwt_id(value: str) -> str | None:
    """读出 JWT 的 jti 用于签发记录；不验签，绝不能拿它做认证。不是 JWT 或没有 jti 时返回 None。"""
    if value.count(".") != 2:
        return None
    try:
        claims = jwt.decode(value, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    jti = claims.get("jti") if isinstance(claims, dict) else None
    if not isinstance(jti, str) or not 0 < len(jti) <= 256 or not jti.isprintable():
        return None
    return jti


class TokenProviderError(Exception):
    """A safe error code, never an upstream body, URL or credential."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TokenProvider(Protocol):
    async def issue(self, request: TokenRequest) -> IssuedToken: ...


class HTTPTokenProvider:
    """Trusted-subject form POST, OAuth Basic auth, standard Bearer token response.

    Configuration is deployment-owned, not a model-supplied URL. TLS verifies the
    provider; the resulting token is for downstream services, never CoreMan auth.
    """

    def __init__(self, config: HTTPTokenProviderConfig) -> None:
        self.config = config

    async def issue(self, request: TokenRequest) -> IssuedToken:
        cfg = self.config
        if not request.subject or not request.audience or request.ttl_seconds <= 0:
            raise TokenProviderError("invalid_request")
        ttl = min(request.ttl_seconds, cfg.max_token_ttl_seconds or request.ttl_seconds)
        form = {
            cfg.subject_field: request.subject,
            cfg.audience_field: request.audience,
            cfg.ttl_field: str(ttl),
        }
        # RFC 6749 encodes each credential before constructing HTTP Basic.
        auth = httpx.BasicAuth(
            quote_plus(cfg.client_id), quote_plus(cfg.client_secret.get_secret_value())
        )
        started = int(time.time())
        try:
            async with (
                asyncio.timeout(ISSUE_TIMEOUT_SECONDS),
                httpx.AsyncClient(
                    timeout=ISSUE_TIMEOUT_SECONDS, trust_env=False, follow_redirects=False
                ) as client,
            ):
                async with client.stream("POST", cfg.token_url, data=form, auth=auth) as response:
                    if response.status_code in (401, 403):
                        raise TokenProviderError("access_denied")
                    if response.status_code != 200:
                        raise TokenProviderError("issuer_unavailable")
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        raw.extend(part)
                        if len(raw) > 65536:
                            raise TokenProviderError("invalid_response")
            body = json.loads(raw)
            value = body.get("access_token") if isinstance(body, dict) else None
            expires = body.get("expires_in") if isinstance(body, dict) else None
            kind = body.get("token_type") if isinstance(body, dict) else None
            if (
                not isinstance(value, str)
                or not 8 <= len(value) <= 16384
                or not value.isascii()
                or any(ord(c) < 33 or ord(c) == 127 for c in value)
                or not isinstance(kind, str)
                or kind.lower() != "bearer"
                or type(expires) is not int
                or not 0 < expires <= ttl
                or started + expires <= time.time()
            ):
                raise TokenProviderError("invalid_response")
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
            # Never pass exception text/response bodies through task logs or model prompts.
            raise TokenProviderError("issuer_unavailable") from None
        return IssuedToken(value, expires, started + expires, "bearer", jwt_id(value))


class BuiltinTokenProvider:
    """CoreMan's existing ES256 issuer (including deployment-owned BOT_JWT keys)."""

    def __init__(
        self,
        session: AsyncSession,
        cipher: Cipher,
        *,
        issuer: str,
        name: str,
        external_key: ExternalKey | None,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.issuer = issuer
        self.name = name
        self.external_key = external_key

    async def issue(self, request: TokenRequest) -> IssuedToken:
        key = await signing_key(self.session, self.cipher, self.external_key)
        value = issue_token(
            key,
            self.cipher,
            issuer=self.issuer,
            login=request.subject,
            name=self.name,
            audience=request.audience,
            ttl=request.ttl_seconds,
        )
        # Locally generated claims only; the downstream service still verifies the signature.
        claims = jwt.decode(value, options={"verify_signature": False})
        return IssuedToken(
            value, request.ttl_seconds, int(claims["exp"]), "cookie", str(claims["jti"])
        )


async def issue_system_token(
    session: AsyncSession,
    cipher: Cipher,
    *,
    system: BusinessSystem,
    subject: str,
    name: str,
    ttl_seconds: int,
    issuer: str,
    external_key: ExternalKey | None,
    providers: Mapping[str, HTTPTokenProviderConfig] | None = None,
) -> IssuedToken:
    """Select the explicitly configured issuer; missing configuration fails closed."""
    audience = system.token_audience or system.key
    if system.key in RESERVED_SYSTEM_KEYS or audience in RESERVED_SYSTEM_KEYS:
        raise TokenProviderError("reserved_audience")
    provider: TokenProvider
    if system.token_provider == "builtin":
        provider = BuiltinTokenProvider(
            session, cipher, issuer=issuer, name=name, external_key=external_key
        )
    else:
        config = (providers or {}).get(system.token_provider)
        if config is None:
            raise TokenProviderError("provider_not_configured")
        provider = HTTPTokenProvider(config)
    return await provider.issue(TokenRequest(subject, audience, ttl_seconds))
