import base64
import os
import secrets
from pathlib import Path

import pytest

from coreman.core.config import Settings, get_settings, reset_settings_cache

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
    assert s.timezone == "Asia/Shanghai"
    assert s.log_level == "INFO"
    assert s.relay_network_mode == "bridge"
    assert s.object_storage == "local"
    assert s.master_key_bytes == b"\x01" * 32
    assert s.bootstrap_admin_username is None


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
