"""ES256 密钥生命周期；验签只使用本地登记的公钥，不跟随 token 的远程 URL。"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import JwtKey

KEY_AAD = "jwt_keys.private_pem_enc"
MAX_TTL = 43200 + 300
KEY_RETENTION = timedelta(hours=24)


async def active_key(session: AsyncSession, cipher: Cipher, *, rotate: bool = False) -> JwtKey:
    # 所有 API/worker 共用事务级锁，空表初始化也不能同时生成两个活跃密钥。
    await session.execute(text("SELECT pg_advisory_xact_lock(721309130006)"))
    row = (await session.execute(select(JwtKey).where(JwtKey.is_active))).scalar_one_or_none()
    if row is not None and not rotate:
        return row
    now = datetime.now(UTC)
    if row is not None:
        await session.execute(
            update(JwtKey).where(JwtKey.is_active).values(is_active=False, retired_at=now)
        )
    key = ec.generate_private_key(ec.SECP256R1())
    kid = uuid.uuid4().hex
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=kid, use="sig", alg="ES256")
    row = JwtKey(
        kid=kid, private_pem_enc=cipher.encrypt(pem, KEY_AAD), public_jwk=jwk, is_active=True
    )
    session.add(row)
    await session.flush()
    return row


async def public_keys(session: AsyncSession) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - KEY_RETENTION
    rows = (
        await session.execute(
            select(JwtKey)
            .where(or_(JwtKey.is_active, JwtKey.retired_at > cutoff))
            .order_by(JwtKey.created_at)
        )
    ).scalars()
    return [row.public_jwk for row in rows]


def issue_token(
    key: JwtKey, cipher: Cipher, *, issuer: str, login: str, name: str, audience: str, ttl: int
) -> str:
    if (
        not login.strip()
        or not audience
        or not issuer
        or not 1 <= ttl <= MAX_TTL
        or not key.is_active
    ):
        raise ValueError("无效的令牌主体、范围或有效期")
    now = int(datetime.now(UTC).timestamp())
    return jwt.encode(
        {
            "iss": issuer,
            "sub": login,
            "name": name,
            "aud": audience,
            "scope": audience,
            "iat": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
        },
        cipher.decrypt(key.private_pem_enc, KEY_AAD),
        algorithm="ES256",
        headers={"kid": key.kid},
    )


async def verify_token(
    session: AsyncSession, token: str, *, issuer: str, audience: str
) -> dict[str, Any]:
    if len(token) > 16384:
        raise jwt.InvalidTokenError("token too long")
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if header.get("alg") != "ES256" or not isinstance(kid, str):
        raise jwt.InvalidTokenError("invalid key or algorithm")
    key = await session.get(JwtKey, kid)
    if key is None or (
        not key.is_active
        and (key.retired_at is None or key.retired_at <= datetime.now(UTC) - KEY_RETENTION)
    ):
        raise jwt.InvalidTokenError("unknown or retired key")
    public = jwt.PyJWK.from_dict(key.public_jwk, algorithm="ES256").key
    claims: dict[str, Any] = jwt.decode(
        token,
        public,
        algorithms=["ES256"],
        issuer=issuer,
        audience=audience,
        options={
            "require": ["iss", "sub", "aud", "scope", "iat", "exp", "jti"],
            "strict_aud": True,
        },
    )
    if (
        claims["scope"] != audience
        or not isinstance(claims["sub"], str)
        or not claims["sub"].strip()
    ):
        raise jwt.InvalidTokenError("invalid scope or subject")
    if (
        type(claims["iat"]) is not int
        or type(claims["exp"]) is not int
        or not 0 < claims["exp"] - claims["iat"] <= MAX_TTL
    ):
        raise jwt.InvalidTokenError("invalid token lifetime")
    return claims
