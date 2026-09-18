"""部署时配置的外部签发方密钥（BOT_JWT_*）。只依赖密码学库，进程配置在启动时就解析校验。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

KID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


@dataclass(frozen=True)
class ExternalKey:
    """外部签发方的签名密钥。

    配置后业务系统令牌改用它签发，kid、iss 与令牌字段都跟该签发方一致，已信任它的业务系统
    不用改动即可验签。它只用于签发：CoreMan 自己验签只认 jwt_keys 表里的密钥，所以同样
    持有这把私钥的外部签发方也冒充不了 CoreMan 管理 API。
    """

    kid: str
    issuer: str
    private_pem: str = field(repr=False)
    public_jwk: dict[str, Any]


def _pem(value: str) -> bytes:
    # .env 与 compose env_file 放不下多行值，允许把换行写成字面量 \n。
    return value.strip().replace("\\n", "\n").encode()


def load_external_key(
    private_pem: str, *, public_pem: str | None, kid: str | None, issuer: str | None
) -> ExternalKey:
    """解析并校验外部签发方密钥；报错信息不带密钥内容。"""
    if not kid or not KID_RE.fullmatch(kid):
        raise ValueError(
            "配置 BOT_JWT_PRIVATE_KEY 时须同时配置 BOT_JWT_KID（1–100 位字母、数字或 ._-）"
        )
    issuer = (issuer or "").strip()
    if not issuer or len(issuer) > 200:
        raise ValueError("配置 BOT_JWT_PRIVATE_KEY 时须同时配置 BOT_JWT_ISSUER（1–200 个字符）")
    try:
        key = serialization.load_pem_private_key(_pem(private_pem), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise ValueError("BOT_JWT_PRIVATE_KEY 不是无口令的 PEM 私钥") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("BOT_JWT_PRIVATE_KEY 须是 EC P-256 私钥（ES256）")
    if public_pem:
        try:
            public = serialization.load_pem_public_key(_pem(public_pem))
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            raise ValueError("BOT_JWT_PUBLIC_KEY 不是 PEM 公钥") from exc
        if (
            not isinstance(public, ec.EllipticCurvePublicKey)
            or public.public_numbers() != key.public_key().public_numbers()
        ):
            raise ValueError("BOT_JWT_PUBLIC_KEY 与 BOT_JWT_PRIVATE_KEY 不是同一对密钥")
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=kid, use="sig", alg="ES256")
    return ExternalKey(kid=kid, issuer=issuer, private_pem=pem, public_jwk=jwk)
