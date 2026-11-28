"""授权服务器的规则：客户端登记（元数据文档或动态注册）、授权码 + PKCE、续期令牌轮换、访问令牌
认证与吊销。

只依赖标准协议（RFC 6749、7009、7591、7636、8252、8414、8707、9728，以及 OAuth Client ID
Metadata Document 草案）。令牌都是不透明的随机串，库里
只存 SHA-256；每次认证都查库并复核成员状态，吊销和停用立即生效。
见 docs/superpowers/specs/2026-11-25-coreman-oauth-mcp-cli.md。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import (
    AuthNonce,
    OAuthAccessToken,
    OAuthClient,
    OAuthGrant,
    User,
)
from coreman.core.relay.safe_transport import PublicTransport
from coreman.core.timeutils import utcnow

ACCESS_PREFIX = "cmo_"
REFRESH_PREFIX = "cmr_"
CLIENT_PREFIX = "cmc_"
CODE_PREFIX = "cmx_"
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(seconds=60)
CONSENT_TTL = timedelta(minutes=10)
CODE_KIND = "oauth_code"
CONSENT_KIND = "oauth_consent"
# scope → 确认页上给人看的说明。没写 scope 时只给 mcp：最小权限。
SCOPES = {
    "mcp": "查业务系统目录（MCP）",
    "api": "以你的身份使用 CoreMan 管理接口（与你在管理台里的权限相同）",
}
DEFAULT_SCOPE = "mcp"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
MAX_REDIRECTS = 5
MAX_NAME = 100
# 同一个 IP 一小时内最多注册这么多客户端：注册不要求登录，防止灌表。
REGISTER_PER_HOUR = 30
# 客户端元数据文档：只取这么大、等这么久，不跟随重定向。
DOCUMENT_MAX = 16_384
DOCUMENT_TIMEOUT = 5.0
_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
_CHALLENGE = re.compile(r"[A-Za-z0-9\-_]{43}")


class OAuthError(Exception):
    """RFC 6749 的错误：`error` 是标准代码，`description` 给人看。"""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def body(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _redirect_parts(uri: str) -> tuple[str, str, str] | None:
    """本机回环回调拆成 (主机, 路径, 查询)；端口按 RFC 8252 不参与比较。不是本机回环返回 None。"""
    if len(uri) > 2000 or any(ch.isspace() for ch in uri):
        return None
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 — 端口不是数字时抛 ValueError
    except ValueError:
        return None
    if parts.scheme != "http" or parts.fragment or parts.username or parts.password:
        return None
    host = parts.netloc.rsplit(":", 1)[0] if not parts.netloc.endswith("]") else parts.netloc
    if host.lower() not in LOOPBACK_HOSTS:
        return None
    return host.lower(), parts.path or "/", parts.query


def redirect_allowed(client: OAuthClient, uri: str) -> bool:
    presented = _redirect_parts(uri)
    return presented is not None and any(
        _redirect_parts(registered) == presented for registered in client.redirect_uris
    )


def parse_scope(raw: str | None) -> str:
    """请求的 scope 规范成空格分隔、排好序的串；有不认识的就拒绝。"""
    names = sorted(set((raw or "").split())) or [DEFAULT_SCOPE]
    unknown = [name for name in names if name not in SCOPES]
    if unknown:
        raise OAuthError("invalid_scope", f"不支持的 scope：{' '.join(unknown)}")
    return " ".join(names)


def canonical_resource(value: str) -> str:
    """资源标识比较用的写法：去掉查询串、片段和末尾的斜杠。"""
    return value.split("#", 1)[0].split("?", 1)[0].rstrip("/")


def check_resources(
    resources: list[str], scope: str, known: dict[str, str], *, cover: bool
) -> list[str]:
    """RFC 8707 的 `resource`：只认 `known`（资源 → 它的 scope）里的资源，而且令牌的 scope 要用得
    上它。`cover`（授权请求）还要求每个 scope 都落在点名的资源里：令牌不会带着别处才用的权限。
    返回规范化后的资源。"""
    names = [canonical_resource(item) for item in resources]
    scopes = set(scope.split())
    for name in names:
        if name not in known:
            raise OAuthError("invalid_target", "resource 不是这台服务器上的资源")
        if known[name] not in scopes:
            raise OAuthError("invalid_target", "授权范围用不到所请求的资源")
    if cover and names and not scopes <= {known[name] for name in names}:
        raise OAuthError("invalid_target", "请求的 scope 不属于所请求的资源")
    return names


def metadata_client_id(value: str) -> bool:
    """是不是「客户端元数据文档」式的 client_id：一个带路径的 https 地址。"""
    if not value.startswith("https://") or len(value) > 2000 or any(c.isspace() for c in value):
        return False
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 — 端口不是数字时抛 ValueError
    except ValueError:
        return False
    segments = parts.path.split("/")
    return bool(
        parts.hostname
        and parts.path not in ("", "/")
        and not (parts.query or parts.fragment or parts.username or parts.password)
        and "." not in segments
        and ".." not in segments
    )


async def fetch_client_document(client_id: str) -> Any:
    """取回客户端元数据文档：只访问公网地址，不跟随重定向，限时限长。"""
    failed = OAuthError(
        "invalid_client", "取不到客户端的元数据文档，请稍后重试或联系客户端的提供方"
    )
    try:
        async with (
            httpx.AsyncClient(
                transport=PublicTransport(), timeout=DOCUMENT_TIMEOUT, follow_redirects=False
            ) as http,
            http.stream("GET", client_id, headers={"Accept": "application/json"}) as reply,
        ):
            if reply.status_code != 200:
                raise failed
            raw = bytearray()
            async for chunk in reply.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > DOCUMENT_MAX:
                    raise failed
    except httpx.HTTPError:
        raise failed from None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise failed from None


async def sync_metadata_client(
    session: AsyncSession, client_id: str, document: Any, ip: str | None
) -> OAuthClient:
    """按刚取回的元数据文档登记或更新客户端。和动态注册一样只收公开客户端、本机回环回调。"""
    if not isinstance(document, dict) or document.get("client_id") != client_id:
        raise OAuthError("invalid_client", "客户端元数据文档里的 client_id 与它的地址不一致")
    name = document.get("client_name")
    uris = document.get("redirect_uris")
    if not isinstance(name, str) or not isinstance(uris, list):
        raise OAuthError("invalid_client", "客户端元数据文档缺少 client_name 或 redirect_uris")
    if document.get("token_endpoint_auth_method", "none") != "none":
        raise OAuthError("invalid_client", "只支持公开客户端（token_endpoint_auth_method=none）")
    loopback = [u for u in dict.fromkeys(uris) if isinstance(u, str) and _redirect_parts(u)]
    if not loopback:
        raise OAuthError(
            "invalid_client",
            "这个客户端没有本机回环回调地址（http://127.0.0.1、localhost 或 [::1]），"
            "不能在这里授权",
        )
    name = "".join(ch for ch in name if ch.isprintable()).strip()[:MAX_NAME] or "未命名客户端"
    client = await session.get(OAuthClient, client_id)
    if client is None:
        client = OAuthClient(client_id=client_id, client_name=name, created_ip=ip)
        session.add(client)
    client.client_name = name
    client.redirect_uris = loopback[:MAX_REDIRECTS]
    await session.flush()
    return client


async def register(session: AsyncSession, metadata: dict[str, Any], ip: str | None) -> OAuthClient:
    """RFC 7591 动态注册：只收公开客户端和本机回环回调。"""
    if not isinstance(metadata, dict):
        raise OAuthError("invalid_client_metadata", "注册信息必须是 JSON 对象")
    method = metadata.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise OAuthError(
            "invalid_client_metadata", "只支持公开客户端（token_endpoint_auth_method=none）"
        )
    grants = metadata.get("grant_types", ["authorization_code", "refresh_token"])
    if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
        raise OAuthError("invalid_client_metadata", "只支持 authorization_code 与 refresh_token")
    responses = metadata.get("response_types", ["code"])
    if responses != ["code"]:
        raise OAuthError("invalid_client_metadata", "只支持 response_type=code")
    uris = metadata.get("redirect_uris")
    if (
        not isinstance(uris, list)
        or not 1 <= len(uris) <= MAX_REDIRECTS
        or not all(isinstance(u, str) and _redirect_parts(u) for u in uris)
    ):
        raise OAuthError(
            "invalid_redirect_uri",
            "回调地址只能是本机回环地址（http://127.0.0.1、localhost 或 [::1]）",
        )
    name = metadata.get("client_name") or "未命名客户端"
    if not isinstance(name, str):
        raise OAuthError("invalid_client_metadata", "client_name 必须是字符串")
    name = "".join(ch for ch in name if ch.isprintable()).strip()[:MAX_NAME] or "未命名客户端"
    if ip is not None:
        recent = await session.scalar(
            select(func.count())
            .select_from(OAuthClient)
            .where(
                OAuthClient.created_ip == ip,
                OAuthClient.created_at > utcnow() - timedelta(hours=1),
            )
        )
        if (recent or 0) >= REGISTER_PER_HOUR:
            raise OAuthError("invalid_client_metadata", "注册过于频繁，请稍后再试", status=429)
    client = OAuthClient(
        client_id=CLIENT_PREFIX + secrets.token_urlsafe(18),
        client_name=name,
        redirect_uris=list(dict.fromkeys(uris)),
        created_ip=ip,
    )
    session.add(client)
    await session.flush()
    return client


@dataclass(frozen=True)
class AuthorizeRequest:
    client: OAuthClient
    redirect_uri: str
    state: str | None
    challenge: str
    scope: str
    resources: tuple[str, ...] = ()


async def check_authorize(
    session: AsyncSession, params: dict[str, str], resources: list[str] | None = None
) -> AuthorizeRequest:
    """校验授权请求。客户端或回调不对时不能跳回去（OAuthError 由调用方渲染成页面）。"""
    client = await session.get(OAuthClient, params.get("client_id") or "")
    if client is None:
        raise OAuthError("invalid_client", "客户端未注册或已失效，请在客户端里重新登录")
    redirect_uri = params.get("redirect_uri") or ""
    if not redirect_allowed(client, redirect_uri):
        raise OAuthError("invalid_request", "回调地址与注册时不一致")
    return AuthorizeRequest(
        client=client,
        redirect_uri=redirect_uri,
        state=params.get("state"),
        challenge=params.get("code_challenge") or "",
        scope=params.get("scope") or "",
        resources=tuple(resources or ()),
    )


def check_redirectable(
    req: AuthorizeRequest, params: dict[str, str], known: dict[str, str] | None = None
) -> None:
    """回调已确认可信之后的校验：这里出错可以按 OAuth 惯例带 error 跳回客户端。"""
    if params.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "只支持 response_type=code")
    if params.get("code_challenge_method") != "S256" or not _CHALLENGE.fullmatch(req.challenge):
        raise OAuthError("invalid_request", "必须使用 PKCE（code_challenge_method=S256）")
    scope = parse_scope(req.scope)
    if req.resources:
        check_resources(list(req.resources), scope, known or {}, cover=True)


async def start_consent(
    session: AsyncSession, req: AuthorizeRequest, user: User, login_id: uuid.UUID
) -> str:
    """建确认页的一次性提交凭证，绑定这位成员和这次登录。"""
    ticket = secrets.token_urlsafe(32)
    session.add(
        AuthNonce(
            kind=CONSENT_KIND,
            value=digest(ticket),
            payload={
                "client_id": req.client.client_id,
                "redirect_uri": req.redirect_uri,
                "state": req.state,
                "challenge": req.challenge,
                "scope": parse_scope(req.scope),
                "resource": [canonical_resource(item) for item in req.resources],
                "user_id": str(user.id),
                "login_id": str(login_id),
            },
        )
    )
    await session.execute(
        delete(AuthNonce).where(
            AuthNonce.kind.in_((CONSENT_KIND, CODE_KIND)),
            AuthNonce.created_at < utcnow() - timedelta(days=1),
        )
    )
    await session.flush()
    return ticket


async def take_consent(
    session: AsyncSession, ticket: str, user: User, login_id: uuid.UUID
) -> dict[str, Any]:
    """取出并作废确认凭证；不是这位成员这次登录发的、或过期了都拒绝。"""
    row = (
        await session.execute(
            delete(AuthNonce)
            .where(AuthNonce.kind == CONSENT_KIND, AuthNonce.value == digest(ticket))
            .returning(AuthNonce)
        )
    ).scalar_one_or_none()
    if (
        row is None
        or utcnow() - row.created_at > CONSENT_TTL
        or row.payload.get("user_id") != str(user.id)
        or row.payload.get("login_id") != str(login_id)
    ):
        raise OAuthError("invalid_request", "授权页已失效，请回到客户端重新发起")
    return dict(row.payload)


async def issue_code(session: AsyncSession, consent: dict[str, Any]) -> str:
    code = CODE_PREFIX + secrets.token_urlsafe(32)
    session.add(
        AuthNonce(
            kind=CODE_KIND,
            value=digest(code),
            payload={
                **{
                    k: consent[k]
                    for k in ("client_id", "redirect_uri", "challenge", "scope", "user_id")
                },
                "resource": consent.get("resource") or [],
            },
        )
    )
    await session.flush()
    return code


@dataclass(frozen=True)
class Issued:
    access_token: str
    refresh_token: str
    scope: str

    def body(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TTL.total_seconds()),
            "refresh_token": self.refresh_token,
            "scope": self.scope,
        }


async def _active_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    user = await session.get(User, user_id)
    if user is None or user.status != "active" or user.source == "bootstrap":
        return None
    return user


async def _mint_access(session: AsyncSession, grant: OAuthGrant) -> str:
    token = ACCESS_PREFIX + secrets.token_urlsafe(32)
    now = utcnow()
    session.add(
        OAuthAccessToken(token_hash=digest(token), grant_id=grant.id, expires_at=now + ACCESS_TTL)
    )
    # 顺带清掉过期一天以上的访问令牌，表不会无限长。
    await session.execute(
        delete(OAuthAccessToken).where(OAuthAccessToken.expires_at < now - timedelta(days=1))
    )
    await session.flush()
    return token


async def exchange_code(
    session: AsyncSession,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    verifier: str,
    resource: str | None = None,
    known: dict[str, str] | None = None,
) -> tuple[Issued, OAuthGrant]:
    invalid = OAuthError("invalid_grant", "授权码无效、已使用或已过期")
    row = (
        await session.execute(
            delete(AuthNonce)
            .where(AuthNonce.kind == CODE_KIND, AuthNonce.value == digest(code))
            .returning(AuthNonce)
        )
    ).scalar_one_or_none()
    # 先把授权码作废并提交：后面任何一步失败，它都不能再用一次。
    await session.commit()
    if row is None or utcnow() - row.created_at > CODE_TTL:
        raise invalid
    payload = row.payload
    if payload.get("client_id") != client_id or payload.get("redirect_uri") != redirect_uri:
        raise invalid
    if not _VERIFIER.fullmatch(verifier):
        raise OAuthError("invalid_grant", "code_verifier 不合规")
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    if not secrets.compare_digest(expected.decode(), str(payload.get("challenge"))):
        raise OAuthError("invalid_grant", "PKCE 校验失败")
    if resource:
        # 换令牌时点名的资源，必须是授权时点过的（授权时没点名就只按 scope 核对）。
        (name,) = check_resources([resource], str(payload["scope"]), known or {}, cover=False)
        authorized = payload.get("resource") or []
        if authorized and name not in authorized:
            raise OAuthError("invalid_target", "resource 与授权时请求的不一致")
    user = await _active_user(session, uuid.UUID(str(payload["user_id"])))
    if user is None:
        raise OAuthError("invalid_grant", "账号已停用")
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    now = utcnow()
    grant = OAuthGrant(
        client_id=client_id,
        user_id=user.id,
        scope=str(payload["scope"]),
        refresh_hash=digest(refresh),
        refresh_expires_at=now + REFRESH_TTL,
        last_used_at=now,
    )
    session.add(grant)
    await session.flush()
    await session.execute(
        update(OAuthClient).where(OAuthClient.client_id == client_id).values(last_used_at=now)
    )
    access = await _mint_access(session, grant)
    return Issued(access, refresh, grant.scope), grant


async def refresh_grant(
    session: AsyncSession,
    *,
    refresh_token: str,
    client_id: str,
    scope: str | None,
    resource: str | None = None,
    known: dict[str, str] | None = None,
) -> tuple[Issued, OAuthGrant]:
    """续期令牌轮换：换出新的一对；上一代再出现说明泄露了，整次授权吊销。"""
    invalid = OAuthError("invalid_grant", "续期令牌无效、已吊销或已过期，请重新登录")
    hashed = digest(refresh_token)
    grant = await session.scalar(
        select(OAuthGrant).where(OAuthGrant.refresh_hash == hashed).with_for_update()
    )
    if grant is None:
        reused = await session.scalar(
            select(OAuthGrant).where(OAuthGrant.previous_refresh_hash == hashed).with_for_update()
        )
        if reused is not None and reused.revoked_at is None:
            reused.revoked_at = utcnow()
            await session.commit()
        raise invalid
    now = utcnow()
    if grant.revoked_at is not None or grant.refresh_expires_at <= now:
        raise invalid
    if grant.client_id != client_id:
        raise invalid
    if scope and not set(parse_scope(scope).split()) <= set(grant.scope.split()):
        raise OAuthError("invalid_scope", "续期不能扩大授权范围")
    if resource:
        check_resources([resource], grant.scope, known or {}, cover=False)
    if await _active_user(session, grant.user_id) is None:
        raise OAuthError("invalid_grant", "账号已停用")
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    grant.previous_refresh_hash = hashed
    grant.refresh_hash = digest(refresh)
    grant.refresh_expires_at = now + REFRESH_TTL
    grant.last_used_at = now
    await session.flush()
    access = await _mint_access(session, grant)
    return Issued(access, refresh, grant.scope), grant


async def revoke(session: AsyncSession, token: str, client_id: str | None = None) -> None:
    """RFC 7009：访问令牌或续期令牌都行，整次授权一起失效；不认识的令牌也算成功。"""
    hashed = digest(token)
    grant_id: uuid.UUID | None = None
    if token.startswith(ACCESS_PREFIX):
        grant_id = await session.scalar(
            select(OAuthAccessToken.grant_id).where(OAuthAccessToken.token_hash == hashed)
        )
    elif token.startswith(REFRESH_PREFIX):
        grant_id = await session.scalar(
            select(OAuthGrant.id).where(OAuthGrant.refresh_hash == hashed)
        )
    if grant_id is None:
        return
    grant = await session.get(OAuthGrant, grant_id, with_for_update=True)
    if grant is None or (client_id and grant.client_id != client_id):
        return
    await revoke_grant(session, grant)


async def revoke_grant(session: AsyncSession, grant: OAuthGrant) -> None:
    if grant.revoked_at is None:
        grant.revoked_at = utcnow()
    await session.execute(delete(OAuthAccessToken).where(OAuthAccessToken.grant_id == grant.id))
    await session.flush()


class Unauthenticated(Exception):
    """Bearer 认不下来；`insufficient_scope` 区分「令牌有效但不能用在这里」。"""

    def __init__(self, error: str = "invalid_token") -> None:
        super().__init__(error)
        self.error = error


def bearer(authorization: str | None) -> str | None:
    """`Authorization: Bearer cmo_…` 里的令牌；不是 CoreMan 访问令牌返回 None。"""
    if not authorization or authorization[:7].lower() != "bearer " or len(authorization) > 512:
        return None
    token = authorization[7:].strip()
    return token if token.startswith(ACCESS_PREFIX) else None


async def authenticate(session: AsyncSession, token: str, scope: str) -> tuple[OAuthGrant, User]:
    """访问令牌 → (授权, 成员)。令牌必须带 `scope`；每次都复核授权与成员状态。"""
    row = await session.execute(
        select(OAuthAccessToken, OAuthGrant)
        .join(OAuthGrant, OAuthGrant.id == OAuthAccessToken.grant_id)
        .where(OAuthAccessToken.token_hash == digest(token))
    )
    found = row.first()
    now = utcnow()
    if found is None:
        raise Unauthenticated()
    access, grant = found
    if access.expires_at <= now or grant.revoked_at is not None:
        raise Unauthenticated()
    if scope not in grant.scope.split():
        raise Unauthenticated("insufficient_scope")
    user = await _active_user(session, grant.user_id)
    if user is None:
        raise Unauthenticated()
    if grant.last_used_at is None or now - grant.last_used_at > timedelta(minutes=5):
        await session.execute(
            update(OAuthGrant).where(OAuthGrant.id == grant.id).values(last_used_at=now)
        )
        # 认证在请求最开始：会话里没有别的改动，先提交，只读请求也能记下最近使用时间。
        await session.commit()
    return grant, user
