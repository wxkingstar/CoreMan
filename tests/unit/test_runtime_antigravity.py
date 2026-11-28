"""Antigravity（agy）的登录探测与额度换算。"""

import importlib
import subprocess

import pytest

module = importlib.import_module("runtime_daemon.antigravity")

USAGE = {
    "groups": [
        {
            "name": "Gemini Models",
            "buckets": [
                {
                    "window": "weekly",
                    "remaining_fraction": 0.797,
                    "reset_time": "2026-12-03T07:20:28Z",
                },
                {"window": "5h", "remaining_fraction": 0.998, "reset_time": "2026-11-28T21:39:05Z"},
            ],
        },
        {
            "name": "Claude and GPT models",
            "buckets": [
                {"window": "weekly", "remaining_fraction": 1, "reset_time": "1970-01-01T00:00:00Z"},
                {"window": "5h", "remaining_fraction": 0.4, "reset_time": "2026-11-28T22:00:00Z"},
                {
                    "window": "monthly",
                    "remaining_fraction": 0.1,
                    "reset_time": "2026-12-01T00:00:00Z",
                },
            ],
        },
    ]
}


def test_limits_take_the_most_used_group_per_window():
    limits = module.antigravity_limits(USAGE)
    assert limits == {
        "seven_day": {"used_percentage": 20.3, "resets_at": "2026-12-03T07:20:28+00:00"},
        "five_hour": {"used_percentage": 60.0, "resets_at": "2026-11-28T22:00:00+00:00"},
    }


def test_limits_ignore_unused_reset_time_and_bad_data():
    untouched = {
        "groups": [
            {
                "buckets": [
                    {"window": "5h", "remaining_fraction": 1, "reset_time": "1970-01-01T00:00:00Z"}
                ]
            }
        ]
    }
    assert module.antigravity_limits(untouched) == {
        "five_hour": {"used_percentage": 0.0, "resets_at": None}
    }
    assert (
        module.antigravity_limits(
            {"groups": [{"buckets": [{"window": "5h", "remaining_fraction": "x"}]}, "bad"]}
        )
        == {}
    )
    assert module.antigravity_limits({}) == {}


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(module, "_antigravity_usage", {})


def test_usage_reads_login_and_caches(monkeypatch, fresh):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        body = '{"status":"SUCCESS","command":{"name":"usage","data":{"groups":[]}}}'
        return subprocess.CompletedProcess(command, 0, body, "")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.antigravity_usage({"HOME": "/h"}) == ("ready", {"groups": []})
    assert module.antigravity_usage({"HOME": "/h"}) == ("ready", {"groups": []})
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["agy", "-p", "/usage", "--output-format", "json"]
    # 未登录时 CLI 会等人输入授权码：不能把终端交给它。
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["timeout"] <= 30
    module.antigravity_usage({"HOME": "/other"})
    assert len(calls) == 2


def test_usage_reports_login_required(monkeypatch, fresh):
    def waiting(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command, 20, output=b"Authentication required. Please visit the URL", stderr=b""
        )

    monkeypatch.setattr(module.subprocess, "run", waiting)
    assert module.antigravity_usage({"HOME": "/h"}) == ("required", {})


def test_usage_unknown_when_cli_fails(monkeypatch, fresh):
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 3, "", "boom"),
    )
    assert module.antigravity_usage({"HOME": "/h"}) == ("unknown", {})


def test_account_reads_active_google_account(tmp_path):
    assert module.antigravity_account({"HOME": str(tmp_path)}) == ""
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini/google_accounts.json").write_text(
        '{"active": "dev@example.com", "old": []}'
    )
    assert module.antigravity_account({"HOME": str(tmp_path)}) == "dev@example.com"
