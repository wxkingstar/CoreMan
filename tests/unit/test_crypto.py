import base64

import pytest
from hypothesis import given
from hypothesis import strategies as st

from coreman.core.crypto import Cipher, DecryptError, is_encrypted

KEY = b"\x02" * 32


def test_roundtrip_and_format() -> None:
    c = Cipher(KEY)
    token = c.encrypt("secret-值", aad="bots.credentials_enc")
    assert token.startswith("enc:v1:")
    raw = base64.b64decode(token.removeprefix("enc:v1:"))
    assert len(raw) == 12 + len("secret-值".encode()) + 16
    assert c.decrypt(token, aad="bots.credentials_enc") == "secret-值"


def test_nonce_is_random() -> None:
    c = Cipher(KEY)
    assert c.encrypt("a", aad="t.c") != c.encrypt("a", aad="t.c")


def test_wrong_aad_or_key_fails() -> None:
    c = Cipher(KEY)
    token = c.encrypt("a", aad="bots.env_vars_enc")
    with pytest.raises(DecryptError):
        c.decrypt(token, aad="bots.credentials_enc")
    with pytest.raises(DecryptError):
        Cipher(b"\x03" * 32).decrypt(token, aad="bots.env_vars_enc")


def test_rejects_bad_format_and_key_length() -> None:
    with pytest.raises(DecryptError):
        Cipher(KEY).decrypt("plain-text", aad="t.c")
    with pytest.raises(ValueError, match="32"):
        Cipher(b"short")


def test_is_encrypted() -> None:
    assert is_encrypted("enc:v1:abc")
    assert not is_encrypted("enc:v2:abc")
    assert not is_encrypted("")
    assert not is_encrypted(None)


@given(st.text(), st.text(min_size=1, max_size=40))
def test_roundtrip_property(plaintext: str, aad: str) -> None:
    c = Cipher(KEY)
    assert c.decrypt(c.encrypt(plaintext, aad=aad), aad=aad) == plaintext
