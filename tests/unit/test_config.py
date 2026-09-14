import base64
import os

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
