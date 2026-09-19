import base64

import pytest
from hypothesis import given
from hypothesis import strategies as st

from coreman.core.crypto import Cipher, DecryptError, is_encrypted, key_id_of

KEY = b"\x02" * 32
OLD_KEY = b"\x05" * 32


def test_roundtrip_and_format() -> None:
    c = Cipher(KEY)
    token = c.encrypt("secret-值", aad="bots.credentials_enc")
    # 新写入一律带密钥标识，轮换时才认得出这一行该用哪把钥匙。
    assert token.startswith("enc:v2:k1:") and key_id_of(token) == "k1"
    raw = base64.b64decode(token.removeprefix("enc:v2:k1:"))
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
    assert is_encrypted("enc:v2:k1:abc")
    assert not is_encrypted("enc:v3:abc")
    assert not is_encrypted("")
    assert not is_encrypted(None)
    assert key_id_of("enc:v1:abc") is None
    assert key_id_of("enc:v2:bad kid:abc") is None


def test_rotation_reads_old_rows_and_writes_with_the_new_key() -> None:
    """轮换的全部意义：不停机、不重加密，旧行照读，新行用新钥匙。"""
    old = Cipher(OLD_KEY, key_id="k0")
    rotated = Cipher(KEY, key_id="k1", previous={"k0": OLD_KEY})
    legacy_v2 = old.encrypt("旧行", aad="bots.env_vars_enc")
    # 轮换前写的 v1 密文（没有标识）同样要能读出来。
    legacy_v1 = "enc:v1:" + base64.b64encode(old.seal("更旧的行", "bots.env_vars_enc")).decode()

    assert rotated.decrypt(legacy_v2, aad="bots.env_vars_enc") == "旧行"
    assert rotated.decrypt(legacy_v1, aad="bots.env_vars_enc") == "更旧的行"
    assert key_id_of(rotated.encrypt("新行", aad="bots.env_vars_enc")) == "k1"
    # 撤下旧密钥之后，还没迁移的旧行就读不出来了——这是明确的失败，不是静默错认。
    with pytest.raises(DecryptError, match="k0"):
        Cipher(KEY, key_id="k1").decrypt(legacy_v2, aad="bots.env_vars_enc")


def test_key_id_is_only_a_lookup_hint_not_an_authenticator() -> None:
    """换掉 kid 不能让另一把密钥解开密文：AAD 仍是表名.列名，GCM 认证不受 kid 影响。"""
    rotated = Cipher(KEY, key_id="k1", previous={"k0": OLD_KEY})
    token = rotated.encrypt("值", aad="bots.env_vars_enc")
    with pytest.raises(DecryptError):
        rotated.decrypt(token.replace("enc:v2:k1:", "enc:v2:k0:"), aad="bots.env_vars_enc")


def test_rejects_bad_key_ids() -> None:
    with pytest.raises(ValueError, match="密钥标识"):
        Cipher(KEY, key_id="有冒号:的")
    with pytest.raises(ValueError, match="重复"):
        Cipher(KEY, key_id="k1", previous={"k1": OLD_KEY})
    with pytest.raises(DecryptError, match="密钥标识"):
        Cipher(KEY).decrypt("enc:v2:k1", aad="t.c")


@given(st.text(), st.text(min_size=1, max_size=40))
def test_roundtrip_property(plaintext: str, aad: str) -> None:
    c = Cipher(KEY)
    assert c.decrypt(c.encrypt(plaintext, aad=aad), aad=aad) == plaintext
