import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from coreman.core.wecom.callback_crypto import CallbackCrypto, CallbackError, parse_xml

KEY = bytes(range(32))
AES_KEY = base64.b64encode(KEY).decode().rstrip("=")


def encrypted_message(
    message: bytes,
    *,
    receiver: str = "ww-test",
    size: int | None = None,
    padding_byte: int | None = None,
) -> tuple[str, str]:
    plain = (
        b"0123456789abcdef"
        + (len(message) if size is None else size).to_bytes(4, "big")
        + message
        + receiver.encode()
    )
    count = 32 - len(plain) % 32
    plain += bytes([count if padding_byte is None else padding_byte]) * count
    cipher = Cipher(algorithms.AES(KEY), modes.CBC(KEY[:16])).encryptor()
    encrypted = base64.b64encode(cipher.update(plain) + cipher.finalize()).decode()
    signature = hashlib.sha1(
        "".join(sorted(("token", "1000", "nonce", encrypted))).encode()
    ).hexdigest()
    return encrypted, signature


def test_callback_decrypt_and_receiver_padding_lengths() -> None:
    crypto = CallbackCrypto("token", AES_KEY, "ww-test")
    raw = b"<xml><Content>hello</Content><AgentID>1000001</AgentID></xml>"
    encrypted, signature = encrypted_message(raw)
    assert (
        crypto.decrypt(encrypted, signature=signature, timestamp="1000", nonce="nonce", now=1001)
        == raw
    )
    assert parse_xml(raw) == {"Content": "hello", "AgentID": "1000001"}
    for options in ({"receiver": "another-corp"}, {"size": 9999}, {"padding_byte": 0}):
        encrypted, signature = encrypted_message(raw, **options)
        with pytest.raises(CallbackError):
            crypto.decrypt(
                encrypted, signature=signature, timestamp="1000", nonce="nonce", now=1001
            )


def test_callback_signature_time_and_xml_fail_closed() -> None:
    crypto = CallbackCrypto("token", AES_KEY, "ww-test")
    encrypted, signature = encrypted_message(b"hello")
    for extra in (
        {"now": 2000},
        {"signature": "0" * 40},
        {"timestamp": "10000"},
        {"nonce": "changed"},
    ):
        args = {"signature": signature, "timestamp": "1000", "nonce": "nonce", "now": 1001} | extra
        with pytest.raises(CallbackError):
            crypto.decrypt(encrypted, **args)
    for raw in (
        '<!DOCTYPE xml [<!ENTITY x "bad">]><xml><Content>&x;</Content></xml>',
        "<xml><Content>1</Content><Content>2</Content></xml>",
        "<other/>",
    ):
        with pytest.raises(CallbackError):
            parse_xml(raw)
