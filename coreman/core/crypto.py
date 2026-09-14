"""库内凭证加密：AES-256-GCM.

格式 enc:v1:<base64(nonce|ciphertext|tag)>，AAD=表名.列名（spec §4.2）。
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
_NONCE_LEN = 12


class DecryptError(ValueError):
    """密文格式错误、密钥不符或 AAD 不符。"""


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)  # type: ignore[union-attr]


class Cipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("加密密钥必须是 32 字节")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, aad: str) -> str:
        nonce = os.urandom(_NONCE_LEN)
        sealed = self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
        return PREFIX + base64.b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, token: str, aad: str) -> str:
        if not is_encrypted(token):
            raise DecryptError("不是 enc:v1 密文")
        try:
            raw = base64.b64decode(token[len(PREFIX) :], validate=True)
        except (ValueError, TypeError) as exc:
            raise DecryptError("密文 base64 非法") from exc
        if len(raw) < _NONCE_LEN + 16:
            raise DecryptError("密文过短")
        nonce, sealed = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        try:
            return self._aead.decrypt(nonce, sealed, aad.encode("utf-8")).decode("utf-8")
        except InvalidTag as exc:
            raise DecryptError("密钥或 AAD 不匹配") from exc
