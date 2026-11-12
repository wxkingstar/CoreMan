"""沙箱里的模型凭证：副本里没有刷新令牌，快到期不起这一轮，节点的登录文件一字不动。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from runtime_daemon import turn_credentials as creds

NOW = 1_800_000_000.0


def jwt(exp: float) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': exp, 'email': 'demo@example.com'})}.sig"


def write(path: Path, data: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    return raw


def test_claude_copy_drops_the_refresh_token_and_other_secrets(tmp_path):
    source = tmp_path / ".claude" / ".credentials.json"
    raw = write(
        source,
        {
            "claudeAiOauth": {
                "accessToken": "access-1",
                "refreshToken": "refresh-1",
                "expiresAt": int((NOW + 3600) * 1000),
                "scopes": ["user:inference"],
                "subscriptionType": "team",
            },
            "mcpOAuth": {"server": {"refreshToken": "mcp-refresh"}},
        },
    )
    got = creds.claude(tmp_path / ".claude", {}, now=NOW)
    assert got.env == {}
    data = json.loads(got.content or b"")
    assert data == {
        "claudeAiOauth": {
            "accessToken": "access-1",
            "expiresAt": int((NOW + 3600) * 1000),
            "scopes": ["user:inference"],
            "subscriptionType": "team",
        }
    }
    assert b"refresh" not in (got.content or b"")
    assert source.read_bytes() == raw


def test_claude_refuses_a_token_about_to_expire(tmp_path):
    write(
        tmp_path / ".credentials.json",
        {
            "claudeAiOauth": {
                "accessToken": "a",
                "refreshToken": "r",
                "expiresAt": (NOW + 60) * 1000,
            }
        },
    )
    with pytest.raises(creds.Unavailable) as caught:
        creds.claude(tmp_path, {}, now=NOW)
    assert caught.value.code == "credential_expiring"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (None, "credential_missing"),
        ("not json", "credential_unreadable"),
        ("[]", "credential_unreadable"),
    ],
)
def test_claude_without_a_usable_file(tmp_path, content, code):
    if content is not None:
        (tmp_path / ".credentials.json").write_text(content)
    with pytest.raises(creds.Unavailable) as caught:
        creds.claude(tmp_path, {}, now=NOW)
    assert caught.value.code == code


def test_environment_logins_pass_one_variable(tmp_path):
    got = creds.claude(tmp_path, {"ANTHROPIC_API_KEY": "k", "PATH": "/x"}, now=NOW)
    assert got.content is None and dict(got.env) == {"ANTHROPIC_API_KEY": "k"}
    assert dict(creds.codex(tmp_path, {"OPENAI_API_KEY": "o"}, now=NOW).env) == {
        "OPENAI_API_KEY": "o"
    }


def test_codex_copy_keeps_access_drops_refresh_and_resets_last_refresh(tmp_path):
    source = tmp_path / "auth.json"
    raw = write(
        source,
        {
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": jwt(NOW + 7200),
                "access_token": jwt(NOW + 7200),
                "refresh_token": "refresh-2",
                "account_id": "acct",
            },
            "last_refresh": "2026-01-01T00:00:00Z",
        },
    )
    got = creds.codex(tmp_path, {}, now=NOW)
    data = json.loads(got.content or b"")
    assert "refresh_token" not in data["tokens"]
    assert data["tokens"]["account_id"] == "acct"
    assert data["last_refresh"].startswith("2027-01-15T")
    assert source.read_bytes() == raw


def test_codex_api_key_file_is_copied_as_is(tmp_path):
    write(tmp_path / "auth.json", {"OPENAI_API_KEY": "sk-test"})
    got = creds.codex(tmp_path, {}, now=NOW)
    assert json.loads(got.content or b"") == {"OPENAI_API_KEY": "sk-test"}


def test_codex_refuses_expiring_or_unreadable_tokens(tmp_path):
    write(tmp_path / "auth.json", {"tokens": {"access_token": jwt(NOW + 10), "refresh_token": "r"}})
    with pytest.raises(creds.Unavailable) as caught:
        creds.codex(tmp_path, {}, now=NOW)
    assert caught.value.code == "credential_expiring"
    write(tmp_path / "auth.json", {"tokens": {"access_token": "opaque"}})
    with pytest.raises(creds.Unavailable) as caught:
        creds.codex(tmp_path, {}, now=NOW)
    assert caught.value.code == "credential_unreadable"


def test_for_provider_follows_the_active_seat(tmp_path):
    seat = tmp_path / "seat"
    write(
        seat / ".credentials.json",
        {"claudeAiOauth": {"accessToken": "seat", "expiresAt": 10**13}},
    )
    got = creds.for_provider("claude", {"CLAUDE_CONFIG_DIR": str(seat)}, tmp_path / "home")
    assert json.loads(got.content or b"")["claudeAiOauth"]["accessToken"] == "seat"
    with pytest.raises(creds.Unavailable):
        creds.for_provider("opencode", {}, tmp_path)


def test_write_is_private_and_skips_environment_logins(tmp_path):
    path = creds.write(tmp_path, creds.Credential(content=b"{}"))
    assert path is not None and path.read_bytes() == b"{}"
    assert path.stat().st_mode & 0o077 == 0
    assert creds.write(tmp_path / "other", creds.Credential(env={"A": "b"})) is None
