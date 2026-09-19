import base64
import os
import secrets
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from coreman.core.config import Settings, get_settings, reset_settings_cache
from coreman.core.crypto import Cipher

VALID_KEY = base64.b64encode(b"\x01" * 32).decode()


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    base = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/coreman",
        "PUBLIC_BASE_URL": "https://coreman.example.com",
        "MASTER_KEY": VALID_KEY,
        "SESSION_SECRET": "s" * 32,
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    s = Settings()
    assert s.log_level == "INFO"
    assert s.object_storage == "local"
    assert s.master_key_bytes == b"\x01" * 32
    assert s.bootstrap_admin_username is None
    # 已删除的死配置不再作为字段存在。
    assert not {"timezone", "relay_network_mode"} & set(Settings.model_fields)


def test_s3_storage_requires_credentials_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OBJECT_STORAGE="s3", S3_BUCKET="bucket")
    with pytest.raises(ValueError, match="S3_ACCESS_KEY"):
        Settings()


def _example_value(key: str) -> str:
    root = Path(__file__).resolve().parents[2]
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(key)


@pytest.mark.parametrize(
    "password", [_example_value("BOOTSTRAP_ADMIN_PASSWORD"), "CHANGEME", "short-pw"]
)
def test_bootstrap_password_rejects_template_and_short_values(
    monkeypatch: pytest.MonkeyPatch, password: str
) -> None:
    _env(monkeypatch, BOOTSTRAP_ADMIN_USERNAME="admin", BOOTSTRAP_ADMIN_PASSWORD=password)
    with pytest.raises(ValueError, match="BOOTSTRAP_ADMIN_PASSWORD") as excinfo:
        Settings()
    assert password not in str(excinfo.value)


def test_bootstrap_password_accepts_generated_or_disabled_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # deploy/coreman 用 secrets.token_urlsafe(32) 生成（43 字符）。
    generated = secrets.token_urlsafe(32)
    _env(monkeypatch, BOOTSTRAP_ADMIN_USERNAME="admin", BOOTSTRAP_ADMIN_PASSWORD=generated)
    assert Settings().bootstrap_admin_password == generated
    # 口令留空表示不启用引导登录；未设用户名时口令不参与校验。
    _env(monkeypatch, BOOTSTRAP_ADMIN_USERNAME="admin", BOOTSTRAP_ADMIN_PASSWORD="")
    Settings()
    _env(monkeypatch, BOOTSTRAP_ADMIN_USERNAME="", BOOTSTRAP_ADMIN_PASSWORD="change-me")
    Settings()


def test_master_key_must_be_32_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, MASTER_KEY=base64.b64encode(b"short").decode())
    with pytest.raises(ValueError, match="MASTER_KEY"):
        Settings()


def test_public_base_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, PUBLIC_BASE_URL="https://x.example.com/")
    assert Settings().public_base_url == "https://x.example.com"


def test_get_settings_cached_and_resettable(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, LOG_LEVEL="DEBUG")
    assert get_settings() is get_settings()
    assert get_settings().log_level == "DEBUG"
    os.environ["LOG_LEVEL"] = "WARNING"
    reset_settings_cache()
    assert get_settings().log_level == "WARNING"


def _pem_pair(curve: ec.EllipticCurve | None = None) -> tuple[str, str]:
    key = ec.generate_private_key(curve or ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


def test_external_jwt_key_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    assert Settings().external_jwt_key is None


def test_external_jwt_key_loads_multiline_or_escaped_pem(monkeypatch: pytest.MonkeyPatch) -> None:
    private, public = _pem_pair()
    for value in (private, private.replace("\n", "\\n")):
        _env(
            monkeypatch,
            BOT_JWT_PRIVATE_KEY=value,
            BOT_JWT_PUBLIC_KEY=public.replace("\n", "\\n"),
            BOT_JWT_KID="legacy-2024",
            BOT_JWT_ISSUER="legacy-issuer",
        )
        key = Settings().external_jwt_key
        assert key is not None
        assert (key.kid, key.issuer) == ("legacy-2024", "legacy-issuer")
        assert key.public_jwk["kid"] == "legacy-2024" and "d" not in key.public_jwk
        assert "PRIVATE" not in repr(key)
    # 公钥可省略（由私钥推出）。
    _env(monkeypatch, BOT_JWT_PRIVATE_KEY=private, BOT_JWT_KID="k", BOT_JWT_ISSUER="i")
    assert Settings().external_jwt_key is not None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"BOT_JWT_KID": ""}, "BOT_JWT_KID"),
        ({"BOT_JWT_KID": "bad kid"}, "BOT_JWT_KID"),
        ({"BOT_JWT_ISSUER": " "}, "BOT_JWT_ISSUER"),
        ({"BOT_JWT_PRIVATE_KEY": "not a pem"}, "PEM 私钥"),
        ({"BOT_JWT_PUBLIC_KEY": _pem_pair()[1]}, "同一对密钥"),
        ({"BOT_JWT_PRIVATE_KEY": _pem_pair(ec.SECP384R1())[0]}, "P-256"),
        ({"BOT_JWT_PRIVATE_KEY": ""}, "缺少 BOT_JWT_PRIVATE_KEY"),
    ],
)
def test_external_jwt_key_misconfiguration_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str], message: str
) -> None:
    private, public = _pem_pair()
    env = {
        "BOT_JWT_PRIVATE_KEY": private,
        "BOT_JWT_PUBLIC_KEY": public,
        "BOT_JWT_KID": "legacy-2024",
        "BOT_JWT_ISSUER": "legacy-issuer",
        **overrides,
    }
    _env(monkeypatch, **env)
    with pytest.raises(ValueError, match=message) as info:
        Settings()
    # 报错不能带出私钥内容。
    assert "PRIVATE KEY" not in str(info.value)


OLD_KEY = base64.b64encode(b"\x04" * 32).decode()


def test_cipher_defaults_to_a_single_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    cipher = Settings().build_cipher()
    assert cipher.key_id == "k1"
    assert cipher.decrypt(cipher.encrypt("值", "bots.env_vars_enc"), "bots.env_vars_enc") == "值"


def test_rotation_keeps_old_rows_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """轮换后的进程必须还能读旧密钥写的行，否则换钥匙就是一次停机重加密。"""
    _env(monkeypatch, MASTER_KEY_ID="k1")
    old = Cipher(base64.b64decode(OLD_KEY), key_id="k0")
    row = old.encrypt("旧行", "bots.credentials_enc")

    _env(monkeypatch, MASTER_KEY_ID="k2", MASTER_KEYS_PREVIOUS=f" k0:{OLD_KEY} ,")
    rotated = Settings().build_cipher()
    assert rotated.key_id == "k2"
    assert rotated.decrypt(row, "bots.credentials_enc") == "旧行"
    assert rotated.encrypt("新行", "bots.credentials_enc").startswith("enc:v2:k2:")


def test_bad_rotation_config_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """配错了要在进程起不来时就知道，而不是读到某一行旧密文才炸。"""
    _env(monkeypatch, MASTER_KEYS_PREVIOUS="k0")
    with pytest.raises(ValueError, match="kid:base64"):
        Settings()
    _env(monkeypatch, MASTER_KEYS_PREVIOUS=f"k0:{base64.b64encode(b'short').decode()}")
    with pytest.raises(ValueError, match="32 字节"):
        Settings()
    _env(monkeypatch, MASTER_KEY_ID="k1", MASTER_KEYS_PREVIOUS=f"k1:{OLD_KEY}")
    with pytest.raises(ValueError, match="重复"):
        Settings()
    _env(monkeypatch, MASTER_KEY_ID="有冒号:的")
    with pytest.raises(ValueError, match="MASTER_KEY_ID"):
        Settings()
