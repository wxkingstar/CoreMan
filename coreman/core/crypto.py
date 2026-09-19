"""库内凭证加密：AES-256-GCM。

两种密文格式并存：

- `enc:v1:<base64(nonce|ciphertext|tag)>`：历史格式，不带密钥标识。只可能由当时唯一的
  那把 `MASTER_KEY` 加密，所以解密时依次拿主密钥和历史密钥试。
- `enc:v2:<kid>:<base64(nonce|ciphertext|tag)>`：新写入一律用这个。`kid` 是查表用的提示，
  不参与认证——AAD 仍是「表名.列名」，换 kid 并不能让别的密钥解开这段密文。

有了 `kid`，轮换 `MASTER_KEY` 不必先停机重加密全库：把旧密钥留在 `MASTER_KEYS_PREVIOUS`
里，新写入用新主密钥，旧行照旧读得出来，按自己的节奏迁移。
"""

from __future__ import annotations

import base64
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
PREFIX_V2 = "enc:v2:"
# 与 BOT_JWT_KID 同一套字符集；kid 出现在密文串里，必须自带边界（不含冒号）。
KID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DEFAULT_KEY_ID = "k1"
_NONCE_LEN = 12
_TAG_LEN = 16


class DecryptError(ValueError):
    """密文格式错误、密钥标识未知、密钥不符或 AAD 不符。"""


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith((PREFIX, PREFIX_V2))  # type: ignore[union-attr]


def key_id_of(value: str) -> str | None:
    """密文的密钥标识；v1 密文没有标识，返回 None。轮换进度统计用。"""
    if not value.startswith(PREFIX_V2):
        return None
    kid = value[len(PREFIX_V2) :].split(":", 1)[0]
    return kid if KID_RE.match(kid) else None


def _aead(key: bytes) -> AESGCM:
    if len(key) != 32:
        raise ValueError("加密密钥必须是 32 字节")
    return AESGCM(key)


class Cipher:
    """一把主密钥负责加密，主密钥加历史密钥共同负责解密。

    Args:
        key: 主密钥，32 字节。新写入的密文都用它。
        key_id: 主密钥的标识，写进密文。同一把密钥在所有进程里必须配同一个标识。
        previous: 历史密钥 `{kid: key}`，只用于解密。不能与主密钥标识重复。
    """

    def __init__(
        self,
        key: bytes,
        *,
        key_id: str = DEFAULT_KEY_ID,
        previous: dict[str, bytes] | None = None,
    ) -> None:
        if not KID_RE.match(key_id):
            raise ValueError("密钥标识只能是 1-64 位字母、数字或 ._-")
        self._key_id = key_id
        self._aead = _aead(key)
        self._keys: dict[str, AESGCM] = {key_id: self._aead}
        for kid, value in (previous or {}).items():
            if not KID_RE.match(kid):
                raise ValueError("历史密钥标识只能是 1-64 位字母、数字或 ._-")
            if kid == key_id:
                raise ValueError(f"历史密钥标识 {kid} 与主密钥标识重复")
            self._keys[kid] = _aead(value)

    @property
    def key_id(self) -> str:
        return self._key_id

    def seal(self, plaintext: str, aad: str) -> bytes:
        """只返回 nonce|ciphertext|tag，不带前缀与密钥标识。

        给需要短串的场景（会话查看链接）用：这类凭据活不过 24 小时，跟着主密钥走即可，
        不值得为它们在 URL 里多背一个 kid。
        """
        nonce = os.urandom(_NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))

    def open(self, raw: bytes, aad: str) -> str:
        """解 `seal` 的输出。没有密钥标识，所以只用主密钥。"""
        return self._open(self._aead, raw, aad)

    def encrypt(self, plaintext: str, aad: str) -> str:
        return f"{PREFIX_V2}{self._key_id}:" + base64.b64encode(self.seal(plaintext, aad)).decode(
            "ascii"
        )

    def decrypt(self, token: str, aad: str) -> str:
        if token.startswith(PREFIX_V2):
            kid, _, body = token[len(PREFIX_V2) :].partition(":")
            if not body or not KID_RE.match(kid):
                raise DecryptError("enc:v2 密文缺少密钥标识")
            aead = self._keys.get(kid)
            if aead is None:
                raise DecryptError(f"未配置密钥标识 {kid} 对应的密钥")
            return self._open(aead, _b64(body), aad)
        if not token.startswith(PREFIX):
            raise DecryptError("不是 enc:v1 / enc:v2 密文")
        raw = _b64(token[len(PREFIX) :])
        # v1 没有标识，只能逐把试；主密钥排在最前，轮换后的常见情况仍是一次命中。
        for aead in self._keys.values():
            try:
                return self._open(aead, raw, aad)
            except DecryptError:
                continue
        raise DecryptError("密钥或 AAD 不匹配")

    @staticmethod
    def _open(aead: AESGCM, raw: bytes, aad: str) -> str:
        if len(raw) < _NONCE_LEN + _TAG_LEN:
            raise DecryptError("密文过短")
        try:
            return aead.decrypt(raw[:_NONCE_LEN], raw[_NONCE_LEN:], aad.encode("utf-8")).decode(
                "utf-8"
            )
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise DecryptError("密钥或 AAD 不匹配") from exc


def _b64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise DecryptError("密文 base64 非法") from exc
