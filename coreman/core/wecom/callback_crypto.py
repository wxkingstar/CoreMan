"""企业微信应用回调协议；签名、PKCS#7、长度与 ReceiveId 全部核验。

协议参考企业微信团队 SDK：
https://github.com/sbzhu/weworkapi_python/blob/master/callback/WXBizMsgCrypt.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAX_BODY = 1024 * 1024


class CallbackError(ValueError):
    pass


def parse_xml(raw: bytes | str) -> dict[str, str]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_BODY:
            raise CallbackError("callback body limit")
        try:
            raw = raw.decode("utf-8")
        except UnicodeError as exc:
            raise CallbackError("invalid XML encoding") from exc
    if len(raw) > MAX_BODY or "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise CallbackError("XML declarations are forbidden")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CallbackError("invalid callback XML") from exc
    if root.tag != "xml" or len(root) > 40:
        raise CallbackError("unexpected XML root")
    values: dict[str, str] = {}
    for child in root:
        if child.tag in values or len(child):
            raise CallbackError("duplicate or nested callback field")
        values[child.tag] = child.text or ""
    return values


class CallbackCrypto:
    def __init__(self, token: str, aes_key: str, corp_id: str):
        if len(aes_key) != 43 or not token or not corp_id:
            raise CallbackError("callback configuration incomplete")
        try:
            self.key = base64.b64decode(aes_key + "=", validate=True)
        except ValueError as exc:
            raise CallbackError("invalid callback key") from exc
        if len(self.key) != 32:
            raise CallbackError("invalid callback key length")
        self.token, self.corp_id = token, corp_id

    def decrypt(
        self,
        encrypted: str,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        now: float | None = None,
    ) -> bytes:
        if (
            not timestamp.isascii()
            or not timestamp.isdecimal()
            or len(timestamp) > 12
            or abs((time.time() if now is None else now) - int(timestamp)) > 600
            or not nonce
            or len(nonce) > 128
            or len(signature) != 40
            or len(encrypted) > MAX_BODY
        ):
            raise CallbackError("invalid callback envelope")
        digest = hashlib.sha1(
            "".join(sorted((self.token, timestamp, nonce, encrypted))).encode()
        ).hexdigest()
        if not hmac.compare_digest(digest.encode(), signature.encode()):
            raise CallbackError("invalid callback signature")
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
            dec = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
            padded = dec.update(ciphertext) + dec.finalize()
            unpad = padding.PKCS7(256).unpadder()
            plain = unpad.update(padded) + unpad.finalize()
            if len(plain) < 20:
                raise CallbackError("invalid callback frame")
            size = int.from_bytes(plain[16:20], "big")
            if size > MAX_BODY or 20 + size >= len(plain):
                raise CallbackError("invalid callback message length")
            if not hmac.compare_digest(plain[20 + size :], self.corp_id.encode()):
                raise CallbackError("invalid callback receiver")
            return plain[20 : 20 + size]
        except (ValueError, OverflowError) as exc:
            raise CallbackError("callback decryption failed") from exc
