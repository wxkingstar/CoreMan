import pytest

from coreman.core.bots.secrets import (
    CREDENTIALS_AAD,
    decrypt_json,
    encrypt_json,
    mask_dict,
    merge_secret_dict,
    validate_credentials,
)
from coreman.core.crypto import Cipher

CIPHER = Cipher(b"\x05" * 32)


def test_roundtrip_and_empty() -> None:
    token = encrypt_json(CIPHER, {"bot_id": "b1", "secret": "s3cret-value"}, CREDENTIALS_AAD)
    assert token.startswith("enc:v2:k1:") and "s3cret" not in token
    assert decrypt_json(CIPHER, token, CREDENTIALS_AAD) == {
        "bot_id": "b1",
        "secret": "s3cret-value",
    }
    assert decrypt_json(CIPHER, "", CREDENTIALS_AAD) == {}


def test_mask_and_merge() -> None:
    masked = mask_dict({"A": "abcdefgh", "B": "xy"})
    assert masked == {"A": "ab••••gh", "B": "••••"}
    merged = merge_secret_dict(
        {"A": "abcdefgh", "B": "xy", "GONE": "1"}, {"A": "ab••••gh", "B": "new", "C": "added"}
    )
    assert merged == {"A": "abcdefgh", "B": "new", "C": "added"}
    with pytest.raises(ValueError, match="C"):
        merge_secret_dict({}, {"C": "••••"})


def test_validate_credentials() -> None:
    validate_credentials("wecom", {"bot_id": "x", "secret": "y"})
    validate_credentials("feishu", {"app_id": "a", "app_secret": "s", "encrypt_key": "k"})
    with pytest.raises(ValueError, match="secret"):
        validate_credentials("wecom", {"bot_id": "x"})
    with pytest.raises(ValueError, match="extra"):
        validate_credentials("wecom", {"bot_id": "x", "secret": "y", "extra": "z"})
