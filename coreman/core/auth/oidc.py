"""通用 OIDC 登录的客户端一侧：发现文档、JWKS、换令牌与 id_token 校验。

只依赖 OpenID Connect Core 与 Discovery 的标准字段，不认任何身份服务的私有约定。发现文档和公钥
在进程内缓存一小时；遇到不认识的 kid 时重新拉一次公钥（身份服务轮换了密钥）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

TIMEOUT = 10.0
CACHE_SECONDS = 3600
LEEWAY_SECONDS = 60
MAX_DOCUMENT = 1_048_576
# 只接受非对称签名：HS* 会让「客户端密钥」变成签名密钥，none 不签名。
ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA"})


class OIDCError(Exception):
    """登录失败的原因（只进日志，不给浏览器看细节）。"""


@dataclass(frozen=True)
class Provider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    algorithms: frozenset[str]


_providers: dict[str, tuple[float, Provider]] = {}
_keys: dict[str, tuple[float, dict[str, Any]]] = {}


def _secure(url: str, issuer: str) -> bool:
    """端点必须是 https；只有身份服务本身是 http（本机开发）时才允许 http。"""
    scheme = urlsplit(url).scheme
    return scheme == "https" or (scheme == "http" and issuer.startswith("http://"))


async def _get_json(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    response = await client.get(url, follow_redirects=False)
    if response.status_code != 200 or len(response.content) > MAX_DOCUMENT:
        raise OIDCError(f"{url} 返回 {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise OIDCError(f"{url} 不是 JSON 对象")
    return data


async def provider(client: httpx.AsyncClient, issuer: str) -> Provider:
    issuer = issuer.rstrip("/")
    cached = _providers.get(issuer)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    doc = await _get_json(client, issuer + "/.well-known/openid-configuration")
    if str(doc.get("issuer", "")).rstrip("/") != issuer:
        raise OIDCError("发现文档里的 issuer 与配置不一致")
    endpoints = {k: doc.get(k) for k in ("authorization_endpoint", "token_endpoint", "jwks_uri")}
    for key, value in endpoints.items():
        if not isinstance(value, str) or not _secure(value, issuer):
            raise OIDCError(f"发现文档缺少或不安全的 {key}")
    declared = doc.get("id_token_signing_alg_values_supported") or ["RS256"]
    algorithms = frozenset(a for a in declared if isinstance(a, str)) & ALLOWED_ALGORITHMS
    if not algorithms:
        raise OIDCError("身份服务没有可接受的 id_token 签名算法")
    found = Provider(
        issuer=issuer,
        authorization_endpoint=str(endpoints["authorization_endpoint"]),
        token_endpoint=str(endpoints["token_endpoint"]),
        jwks_uri=str(endpoints["jwks_uri"]),
        algorithms=algorithms,
    )
    _providers[issuer] = (time.monotonic(), found)
    return found


async def _key(client: httpx.AsyncClient, found: Provider, kid: str | None) -> dict[str, Any]:
    for refresh in (False, True):
        cached = _keys.get(found.jwks_uri)
        if refresh or not cached or time.monotonic() - cached[0] >= CACHE_SECONDS:
            cached = (time.monotonic(), await _get_json(client, found.jwks_uri))
            _keys[found.jwks_uri] = cached
        keys = [k for k in cached[1].get("keys") or [] if isinstance(k, dict)]
        matched = [k for k in keys if kid is None or k.get("kid") == kid]
        if len(matched) == 1:
            return matched[0]
    raise OIDCError("找不到 id_token 的签名公钥")


async def exchange(
    client: httpx.AsyncClient,
    found: Provider,
    *,
    code: str,
    redirect_uri: str,
    verifier: str,
    client_id: str,
    client_secret: str,
) -> str:
    """授权码换令牌（client_secret_basic），返回 id_token。"""
    response = await client.post(
        found.token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        auth=(client_id, client_secret),
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    if response.status_code != 200:
        raise OIDCError(f"换令牌失败：{response.status_code}")
    token = response.json().get("id_token")
    if not isinstance(token, str):
        raise OIDCError("换令牌的响应里没有 id_token（scope 需要包含 openid）")
    return token


async def verify(
    client: httpx.AsyncClient, found: Provider, token: str, *, client_id: str, nonce: str
) -> dict[str, Any]:
    """校验 id_token：签名、iss、aud（多个受众时 azp 必须是自己）、exp、iat、nonce。"""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise OIDCError("id_token 格式不对") from exc
    algorithm = header.get("alg")
    if algorithm not in found.algorithms:
        raise OIDCError(f"不接受的签名算法 {algorithm}")
    jwk = await _key(client, found, header.get("kid"))
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            jwt.PyJWK(jwk, algorithm=algorithm).key,
            algorithms=[algorithm],
            audience=client_id,
            issuer=found.issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise OIDCError(f"id_token 校验失败：{exc}") from exc
    audience = claims.get("aud")
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != client_id:
        raise OIDCError("id_token 的 azp 不是本客户端")
    if claims.get("nonce") != nonce:
        raise OIDCError("id_token 的 nonce 不匹配")
    return claims


def email_of(claims: dict[str, Any]) -> str | None:
    """用来对应成员的邮箱：身份服务明确说没验证过的不认。"""
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        return None
    if claims.get("email_verified") is False:
        return None
    return email.strip().lower()
